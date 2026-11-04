from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from pipeline.shared.collections import collection_metadata
from pipeline.shared.core import (
    DomainError,
    atomic_json,
    digest,
    executable_path,
    now,
)

from .conversion_options import normalize_options, provider_capabilities
from .datasets import TARGET_FORMATS
from .episode_validation import EpisodeValidationError, validate_episode_policy
from .merges import MergeRunner
from .providers import RuntimeCatalog
from .quality import DOCTOR_CHECK_IDS, doctor_gate
from .storage import (
    data_directory,
    prepare_storage,
    share_data_path,
    share_data_tree,
    storage_path,
)
from .subtask_export import SubtaskExportRunner
from .trims import TrimRunner
from .version_conversions import VersionConversionRunner


class JobRunner:
    def __init__(self, settings, ingest):
        self.settings, self.ingest, self.store = settings, ingest, ingest.store
        self.catalog = RuntimeCatalog(settings)
        self.lock = threading.RLock()
        self.shutdown_requested = threading.Event()
        self.merges = MergeRunner(settings, self)
        self.version_conversions = VersionConversionRunner(settings, self)
        self.subtask_exports = SubtaskExportRunner(settings, self)
        self.trims = TrimRunner(settings, self)

    def recover(self):
        for job in self.store.all("job"):
            if job["state"] == "RUNNING":
                job.update(
                    state="INTERRUPTED",
                    error="Worker 重启；可显式重试，既有工作目录作为诊断数据保留",
                    finished_at=now(),
                )
                self.store.put("job", job["id"], job)

    def enqueue(
        self,
        uid,
        target_format="v3.0",
        run_doctor=True,
        instruction=None,
        conversion_options=None,
        invalid_episode_policy="fail",
        trim_idle_frames=False,
        trim_static_intervals=False,
    ):
        return self._enqueue(
            [uid],
            target_format,
            run_doctor,
            instruction,
            conversion_options=conversion_options,
            invalid_episode_policy=invalid_episode_policy,
            trim_idle_frames=trim_idle_frames,
            trim_static_intervals=trim_static_intervals,
        )

    def enqueue_collection(
        self,
        collection_id,
        instruction,
        target_format="v3.0",
        run_doctor=True,
        upload_ids=None,
        conversion_options=None,
        invalid_episode_policy="fail",
        trim_idle_frames=False,
        trim_static_intervals=False,
    ):
        if upload_ids is None:
            upload_ids = []
            for row in self.store.all("upload"):
                if row["state"] != "COMMITTED":
                    continue
                try:
                    metadata = collection_metadata(
                        self.ingest.manifest(row), row["target"], row["prefix"]
                    )
                except (DomainError, OSError, ValueError, KeyError):
                    continue
                if metadata["collection_id"] == collection_id:
                    upload_ids.append(row["id"])
        return self._enqueue(
            upload_ids,
            target_format,
            run_doctor,
            instruction,
            collection_id,
            conversion_options=conversion_options,
            invalid_episode_policy=invalid_episode_policy,
            trim_idle_frames=trim_idle_frames,
            trim_static_intervals=trim_static_intervals,
        )

    def _enqueue(
        self,
        upload_ids,
        target_format,
        run_doctor,
        instruction,
        collection_id=None,
        *,
        conversion_options=None,
        invalid_episode_policy="fail",
        trim_idle_frames=False,
        trim_static_intervals=False,
    ):
        policy = validate_episode_policy(invalid_episode_policy)
        if (
            target_format not in TARGET_FORMATS
            or type(run_doctor) is not bool
            or type(trim_idle_frames) is not bool
            or type(trim_static_intervals) is not bool
        ):
            raise DomainError(
                "目标格式必须为 v3.0 / v2.1，run_doctor / trim_idle_frames / "
                "trim_static_intervals 必须为布尔值",
                400,
            )
        if trim_idle_frames and trim_static_intervals:
            raise DomainError(
                "“删除首尾静止帧”与“删除全程静止区间”不能同时开启", 400
            )
        if (
            (trim_idle_frames or trim_static_intervals)
            and self.settings.processing_mode == "demo"
        ):
            raise DomainError(
                "演示模式不生成 LeRobot 数据集，无法删除静止帧", 400
            )
        if (
            not upload_ids
            or len(upload_ids) > 1000
            or len(set(upload_ids)) != len(upload_ids)
        ):
            raise DomainError("请选择 1–1000 段不重复的已完成采集", 400)
        with self.lock:
            uploads = [self.store.get("upload", uid) for uid in upload_ids]
            uploads.sort(key=lambda row: (row["created_at"], row["id"]))
            manifests, metadata, runtime_ids = [], [], []
            for upload in uploads:
                if upload["state"] != "COMMITTED":
                    raise DomainError("只有已提交原始数据可以转换")
                manifest = self.ingest.manifest(upload, fresh=True)
                runtime = self.catalog.for_stored_record(upload, manifest)
                runtime_ids.append(runtime.id)
                manifests.append(manifest)
                metadata.append(
                    collection_metadata(manifest, upload["target"], upload["prefix"])
                )
            if len(set(runtime_ids)) != 1:
                raise DomainError(
                    "批量转换不允许混合不同机器人 Runtime，请按机器人类型分别提交", 400
                )
            runtime = self.catalog.get(runtime_ids[0])
            provider = self.catalog.provider(runtime)
            options = normalize_options(
                conversion_options, provider_capabilities(provider, runtime.profile)
            )
            cid = collection_id or metadata[0]["collection_id"]
            if any(m["collection_id"] != cid for m in metadata):
                raise DomainError(
                    "批量采集必须属于同一数据集、机器人配置和存储目标", 400
                )
            if instruction is None:
                instruction = manifests[0]["instruction"]
            if (
                not isinstance(instruction, str)
                or not 1 <= len(instruction.strip()) <= 2000
            ):
                raise DomainError("请在转换时填写 1–2000 字符的 instruction", 400)
            instruction = instruction.strip()
            # The Runtime and its Profile are both Server-owned. Provider code is
            # selected from the static registry, never from the Client manifest.
            profile = runtime.profile
            key = digest(
                {
                    "inputs": [(u["id"], u["manifest_hash"]) for u in uploads],
                    "collection_id": cid,
                    "instruction": instruction,
                    "runtime_id": runtime.id,
                    "provider": provider.id,
                    "profile_hash": runtime.profile_hash,
                    "profile": profile,
                    "mode": self.settings.processing_mode,
                    "target_format": target_format,
                    "run_doctor": run_doctor,
                    **({"conversion_options": options} if options else {}),
                    **({"invalid_episode_policy": policy} if policy != "fail" else {}),
                    **({"trim_idle_frames": True} if trim_idle_frames else {}),
                    **(
                        {"trim_static_intervals": True}
                        if trim_static_intervals
                        else {}
                    ),
                    "pipeline": "runtime-provider-v2",
                    "encoding": "libx264-yuv420p",
                    "letools": "1c417cc9c205f85cd01a5e5860646f1594738354",
                }
            )
            for row in self.store.all("job"):
                if row["key"] == key and row["state"] not in {
                    "FAILED",
                    "INTERRUPTED",
                    "QUARANTINED",
                    "BLOCKED",
                    "DELETING",
                    "DELETE_FAILED",
                    "DELETED",
                }:
                    return row
            job = {
                "id": str(uuid.uuid4()),
                "key": key,
                "upload_id": uploads[0]["id"],
                "upload_ids": [u["id"] for u in uploads],
                "source_manifest_hashes": {
                    u["id"]: u["manifest_hash"] for u in uploads
                },
                "collection_id": cid,
                "dataset_name": metadata[0]["dataset_name"],
                "source_count": len(uploads),
                "invalid_episode_policy": policy,
                "state": "QUEUED",
                "stage": "等待 Worker",
                "created_at": now(),
                "runtime_id": runtime.id,
                "provider": provider.id,
                "profile_hash": runtime.profile_hash,
                "raw_formats": list(runtime.raw_formats),
                "accepted_capture_profile_hashes": list(
                    dict.fromkeys(
                        [
                            *runtime.accepted_capture_profile_hashes,
                            *self.settings.accepted_capture_profile_hashes,
                        ]
                    )
                ),
                "profile": profile,
                "processing_mode": self.settings.processing_mode,
                "target_format": target_format,
                "run_doctor": run_doctor,
                "instruction": instruction,
                "conversion_options": options,
                "trim_idle_frames": trim_idle_frames,
                "trim_static_intervals": trim_static_intervals,
                "capture_mode": manifests[0]["capture_mode"],
                "error": None,
            }
            job["robot_type"] = runtime.robot_type
            for field in ("data_layout",):
                if field in uploads[0]:
                    job[field] = uploads[0][field]
            self.store.put("job", job["id"], job)
            return job

    def runtime_for_job(self, job):
        """Resolve a trusted Runtime, including jobs created before phase two."""
        if job.get("runtime_id"):
            return self.catalog.get(job["runtime_id"])
        upload_id = job.get("upload_id") or (job.get("upload_ids") or [None])[0]
        if not upload_id:
            raise DomainError("任务没有可使用源采集，无法解析机器人 Runtime", 409)
        upload = self.store.get("upload", upload_id)
        manifest = self.ingest.manifest(upload)
        runtime = self.catalog.for_stored_record(upload, manifest)
        job["runtime_id"] = runtime.id
        job["provider"] = runtime.provider
        job["profile_hash"] = runtime.profile_hash
        job["raw_formats"] = list(runtime.raw_formats)
        job["accepted_capture_profile_hashes"] = list(
            dict.fromkeys(
                [
                    *runtime.accepted_capture_profile_hashes,
                    *self.settings.accepted_capture_profile_hashes,
                ]
            )
        )
        return runtime

    def save(self, job, **updates):
        job.update(**updates, updated_at=now())
        self.store.put("job", job["id"], job)

    def command(self, args, work, name, allocation=None, *, timeout=None, cancel_event=None):
        stdout, stderr = work / f"{name}.json", work / f"{name}.stderr.log"
        # An allocation reused across one job's steps avoids paying the Slurm
        # queue latency three times for convert / validate / doctor.
        argv = self._scheduled(args, allocation=allocation)
        with stdout.open("wb") as out, stderr.open("wb") as err:
            share_data_path(stdout)
            share_data_path(stderr)
            env = self.settings.tool_environment()
            process = subprocess.Popen(
                argv, stdout=out, stderr=err, start_new_session=True, cwd=work, env=env
            )
            try:
                deadline = time.monotonic() + (
                    self.settings.command_timeout if timeout is None else timeout
                )
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("命令已取消")
                    if self.shutdown_requested.wait(0.25):
                        raise InterruptedError("Worker 收到停止请求")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("命令执行超时")
                code = process.returncode
            except BaseException:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise
        return code, stdout, stderr

    def _deep_validate(self, executable: str, work: Path, allocation: str | None):
        """Validate the converted dataset, retrying only a publish race.

        A distributed filesystem caches directory metadata for about a second,
        so a check that starts the instant conversion publishes its dataset can
        miss ``meta/info.json`` and report ENOENT. A real validation failure
        must fail without retrying.
        """

        for attempt in range(3):
            code, report, err = self.command(
                [executable, "validate", str(work / "dataset"), "--deep"],
                work,
                "validate",
                allocation=allocation,
            )
            if not code:
                return
            missing = "No such file" in report.read_text(errors="replace")
            if not missing or attempt == 2:
                raise DomainError(
                    f"结构验证失败：{err.read_text(errors='replace')[-2000:]}"
                )
            time.sleep(2)

    def _open_allocation(self, work: Path) -> str | None:
        """Reserve one Slurm allocation for a job, or reuse the caller's.

        ``salloc --no-shell`` grants an allocation and exits immediately; the
        allocation stays alive until it is explicitly cancelled, and each step
        then runs inside it with ``srun --jobid``. That enters the queue once per
        job instead of once per command. Without Slurm this is a no-op.
        """

        config = self.settings.slurm or {}
        if not config.get("enabled"):
            return None
        if os.environ.get("SLURM_JOB_ID"):
            return os.environ["SLURM_JOB_ID"]
        salloc = executable_path("salloc")
        if salloc is None:
            raise DomainError("配置了 Slurm 但找不到 salloc", 503)
        job_name = str(config.get("job_name", "bodypipeline-convert"))
        argv = [
            salloc,
            "--no-shell",
            "--partition",
            str(config.get("partition", "dev")),
            "--job-name",
            job_name,
            "--cpus-per-task",
            str(config.get("cpus_per_task", 16)),
            "--mem",
            str(config.get("mem", "64G")),
            "--time",
            str(config.get("time", "02:00:00")),
        ]
        if config.get("nodelist"):
            argv += ["--nodelist", str(config["nodelist"])]
        if config.get("extra_args"):
            argv += [str(value) for value in config["extra_args"]]
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=900,
            cwd=work,
            env=self.settings.tool_environment(),
        )
        if result.returncode != 0:
            raise DomainError(
                "Slurm 分配失败：" + (result.stderr or result.stdout)[-800:]
            )
        job_id = self._allocation_id(job_name, work)
        if job_id is None:
            raise DomainError("Slurm 未返回分配编号")
        self.allocation_id = job_id
        return job_id

    def _allocation_id(self, job_name: str, work: Path) -> str | None:
        """The newest pending/running allocation with this job name and directory."""

        squeue = executable_path("squeue")
        if squeue is None:
            return None
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            result = subprocess.run(
                [squeue, "--me", "--name", job_name, "--noheader", "--format=%i %T"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
            for row in sorted(rows, key=lambda r: -int(r[0])):
                if len(row) >= 2 and row[1] in {"PENDING", "RUNNING", "CONFIGURING"}:
                    return row[0]
            time.sleep(2)
        return None

    def _close_allocation(self):
        job_id = getattr(self, "allocation_id", None)
        if not job_id:
            return
        self.allocation_id = None
        scancel = executable_path("scancel")
        if scancel is None:
            return
        try:
            subprocess.run([scancel, job_id], capture_output=True, timeout=60)
        except subprocess.SubprocessError:
            pass

    def _conversion_parallelism(self) -> dict:
        """Worker counts for this conversion, bounded by the CPUs available.

        ``os.sched_getaffinity`` reports the allocation's CPUs inside a Slurm
        step, so the same numbers hold when the pipeline is run without Slurm.
        """

        try:
            cpus = len(os.sched_getaffinity(0))
        except AttributeError:
            cpus = os.cpu_count() or 1
        config = self.settings.slurm or {}
        configured = int(config.get("conversion_workers") or 0)
        workers = configured or max(1, min(cpus, 8))
        return {
            "convert_workers": workers,
            "video_workers": workers,
            "video_codec_threads": max(1, cpus // max(1, workers)),
        }

    def _scheduled(self, args, allocation=None):
        """Wrap a heavy command in an Slurm allocation when one is configured.

        Conversion is CPU-bound (JPEG/H.264 encoding across every episode and
        camera), so it runs inside its own allocation instead of competing with
        whatever else the login node is doing. The wrapper is transparent: exit
        code, stdout and stderr belong to the inner command, and ``srun``
        propagates its signal so cancelling the server still stops the work.

        ``allocation`` reuses an existing ``SLURM_JOB_ID`` so a job's convert,
        validate and doctor steps share one allocation and queue once.
        """

        config = self.settings.slurm or {}
        if not config.get("enabled"):
            return list(args)
        srun = executable_path("srun")
        if srun is None:
            raise DomainError("配置了 Slurm 但找不到 srun", 503)
        reusable = allocation or os.environ.get("SLURM_JOB_ID")
        if reusable:
            return [srun, "--jobid", str(reusable), *args]
        argv = [
            srun,
            "--partition",
            str(config.get("partition", "dev")),
            "--job-name",
            str(config.get("job_name", "bodypipeline-convert")),
            "--cpus-per-task",
            str(config.get("cpus_per_task", 16)),
            "--mem",
            str(config.get("mem", "64G")),
            "--time",
            str(config.get("time", "02:00:00")),
            # Do NOT redirect srun's output: doctor writes its JSON report to
            # stdout, and --output /dev/null discards it, leaving an empty file.
            "--exact",
        ]
        if config.get("nodelist"):
            argv += ["--nodelist", str(config["nodelist"])]
        if config.get("extra_args"):
            argv += [str(value) for value in config["extra_args"]]
        return argv + list(args)

    def tick(self):
        if self.settings.auto_process:
            for upload in self.store.all("upload"):
                if (
                    upload["state"] == "COMMITTED"
                    and not upload.get("imported")
                    and upload.get("instruction", "").strip()
                    and not any(
                        upload["id"] in j.get("upload_ids", [j.get("upload_id")])
                        for j in self.store.all("job")
                    )
                ):
                    self.enqueue(upload["id"])
        queued = [j for j in self.store.all("job") if j["state"] == "QUEUED"]
        if queued:
            self.run(queued[-1])
            return True
        return False

    def run(self, job):
        if job.get("operation") == "merge":
            self.merges.run(job)
            return
        if job.get("operation") == "version_conversion":
            self.version_conversions.run(job)
            return
        if job.get("operation") == "subtask_export":
            self.subtask_exports.run(job)
            return
        if job.get("operation") == "stationary_trim":
            self.trims.run(job)
            return
        self.save(
            job,
            state="RUNNING",
            stage="原始数据核验与时间对齐",
            started_at=now(),
        )
        # A failed job must not leave an allocation holding CPUs until its wall
        # clock runs out, so every exit path releases it.
        self.allocation_id = None
        try:
            prepare_storage(self.settings, job)
            work = storage_path(self.settings, job, "work", job["id"])
            self.save(job, work_path=str(work))
            data_directory(self.settings, job, "work", job["id"], exist_ok=False)
            runtime = self.runtime_for_job(job)
            provider = self.catalog.provider(runtime)
            profile = job.get("profile") or runtime.profile
            self.save(
                job,
                profile=profile,
                runtime_id=runtime.id,
                provider=provider.id,
                profile_hash=job.get("profile_hash", runtime.profile_hash),
                raw_formats=job.get("raw_formats", list(runtime.raw_formats)),
            )
            synthetic = job["capture_mode"] == "mock"
            blockers = provider.profile_blockers(profile) if not synthetic else []
            if blockers:
                self.save(
                    job,
                    state="BLOCKED",
                    stage="等待机器人配置核实",
                    blockers=blockers,
                    error="；".join(item["message"] for item in blockers),
                    finished_at=now(),
                )
                return
            if not synthetic:
                provider.validate_profile(profile)
            options = normalize_options(
                job.get("conversion_options"), provider_capabilities(provider, profile)
            )
            from pipeline.shared.core import confined, file_hash

            ids = job.get("upload_ids", [job["upload_id"]])
            policy = validate_episode_policy(job.get("invalid_episode_policy", "fail"))
            reports, robot_reports, sources = [], [], []
            episode_diagnostics = []
            aligned = work / "aligned"
            aligned.mkdir()
            dimensions = None
            # Readers sort episode paths lexically. Preserve numeric ordering
            # for batches containing ten or more output episodes.
            index_width = len(str(len(ids) - 1))
            for index, uid in enumerate(ids):
                if self.shutdown_requested.is_set():
                    raise InterruptedError("Worker 收到停止请求")
                self.save(
                    job,
                    stage=f"核验与对齐 episode {index + 1}/{len(ids)}",
                    current_upload_id=uid,
                )
                try:
                    upload = self.store.get("upload", uid)
                    manifest = self.ingest.manifest(upload, fresh=True)
                    if upload["state"] != "COMMITTED" or upload[
                        "manifest_hash"
                    ] != job.get("source_manifest_hashes", {}).get(
                        uid, upload["manifest_hash"]
                    ):
                        raise DomainError("源采集状态或 Manifest 与提交快照不一致")
                    source_runtime = self.catalog.for_stored_record(upload, manifest)
                    if source_runtime.id != runtime.id:
                        raise DomainError(
                            f"源采集 Runtime {source_runtime.id!r} 与任务 Runtime "
                            f"{runtime.id!r} 不一致"
                        )
                    self.catalog.validate_capture(
                        runtime, manifest, require_compatible_hash=False
                    )
                    metadata = collection_metadata(
                        manifest, upload["target"], upload["prefix"]
                    )
                    if (
                        job.get("collection_id")
                        and metadata["collection_id"] != job["collection_id"]
                    ):
                        raise DomainError("源采集不属于任务数据集")
                    if (
                        manifest["capture_mode"] != job["capture_mode"]
                        or manifest["profile"]["robot_type"] != profile["robot_type"]
                    ):
                        raise DomainError("采集模式或机器人类型与转换配置不匹配")
                    accepted_hashes = {
                        job.get("profile_hash", digest(profile)),
                        *job.get(
                            "accepted_capture_profile_hashes",
                            runtime.accepted_capture_profile_hashes,
                        ),
                    }
                    if (
                        not upload.get("imported")
                        and manifest["profile_hash"] not in accepted_hashes
                    ):
                        raise DomainError(
                            "上传 Profile 与服务器批准版本不同；请审核后将兼容摘要加入该 Runtime"
                        )
                    raw = Path(upload["raw_path"])
                    # An imported upstream episode registers the HDF5 file
                    # itself as the capture, so hashes resolve against its
                    # parent directory.
                    raw_root = raw if raw.is_dir() else raw.parent
                    source_work = work / "sources" / str(index)
                    for f in manifest["files"]:
                        if file_hash(confined(raw_root, f["path"])) != f["sha256"]:
                            raise DomainError(f"原始文件摘要变化：{f['path']}")
                    prepared = provider.prepare(
                        raw,
                        source_work / "aligned",
                        profile,
                        **({"options": options} if options else {}),
                    )
                    aligned_path = Path(
                        prepared.pop("aligned_path", source_work / "aligned/episode0")
                    )
                    aligned_kind = prepared.pop("aligned_kind", "directory")
                    robot_report = provider.quality(
                        aligned_path, profile, synthetic=synthetic
                    )
                    shapes = {
                        k: v["dimensions"]
                        for k, v in prepared["streams"].items()
                        if v["dimensions"]
                    }
                    if dimensions is not None and shapes != dimensions:
                        raise EpisodeValidationError(
                            "批量采集的数据维度不一致", code="incompatible_dimensions"
                        )
                    dimensions = shapes
                    output_index = len(reports)
                    episode_name = f"episode{output_index:0{index_width}d}"
                    target = (
                        aligned / f"{episode_name}.hdf5"
                        if aligned_kind == "hdf5"
                        else aligned / episode_name
                    )
                    os.replace(aligned_path, target)
                    reports.append(
                        {
                            **prepared,
                            "episode_index": output_index,
                            "source_episode_index": index,
                            "upload_id": uid,
                        }
                    )
                    robot_reports.append(
                        {
                            **robot_report,
                            "episode_index": output_index,
                            "source_episode_index": index,
                            "upload_id": uid,
                        }
                    )
                    sources.append(
                        {
                            "episode_index": output_index,
                            "source_episode_index": index,
                            "upload_id": uid,
                            "session_id": manifest["session_id"],
                            "robot_id": manifest["robot_id"],
                            "manifest_hash": upload["manifest_hash"],
                            "source_instruction": manifest["instruction"],
                        }
                    )
                    episode_diagnostics.append(
                        {
                            "episode_index": index,
                            "upload_id": uid,
                            "passed": True,
                            "output_episode_index": output_index,
                            "error": None,
                        }
                    )
                except InterruptedError:
                    raise
                except Exception as exc:
                    episode_diagnostics.append(
                        {
                            "episode_index": index,
                            "upload_id": uid,
                            "passed": False,
                            "error": str(exc),
                            "skippable": isinstance(exc, EpisodeValidationError),
                            "error_code": getattr(
                                exc, "reason_code", "processing_error"
                            ),
                            "details": getattr(exc, "details", {}),
                            "output_episode_index": None,
                        }
                    )
                self.save(job, episode_diagnostics=episode_diagnostics)
            failed = [row for row in episode_diagnostics if not row["passed"]]
            skipped = [row for row in failed if policy == "skip" and row["skippable"]]
            fatal = [row for row in failed if policy == "fail" or not row["skippable"]]
            selection = {
                "policy": policy,
                "input_count": len(ids),
                "accepted_count": len(reports),
                "skipped_count": len(skipped),
                "failed_count": len(fatal),
                "skipped": skipped,
            }
            self.save(job, selection_report=selection)
            atomic_json(
                work / "episode-selection.json",
                {**selection, "episodes": episode_diagnostics},
            )
            if fatal or not reports:
                summary = "；".join(
                    f"episode {row['episode_index']} / 采集 {row['upload_id']}：{row['error']}"
                    for row in failed
                )
                raise DomainError(
                    f"{len(failed)}/{len(ids)} 个 episode 未通过核验与对齐：{summary}"
                    + ("；没有合格采集可供转换" if not reports else "")
                )
            job["alignment_report"] = (
                reports[0]
                if len(reports) == 1
                else {
                    "frames": sum(r["frames"] for r in reports),
                    "fps": reports[0].get("fps", profile.get("output_fps")),
                    "episode_count": len(reports),
                    "episodes": reports,
                }
            )
            job["robot_report"] = (
                robot_reports[0]
                if len(robot_reports) == 1
                else {
                    "status": "SYNTHETIC" if synthetic else "PASS",
                    "joint_limits_enforced": False,
                    "episodes": robot_reports,
                }
            )
            atomic_json(work / "alignment-report.json", job["alignment_report"])
            atomic_json(work / "robot-report.json", job["robot_report"])
            atomic_json(
                work / "lineage.json",
                {
                    "manifest_hash": sources[0]["manifest_hash"],
                    "sources": sources,
                    "episode_selection": selection,
                    "collection_id": job.get("collection_id"),
                    "dataset_name": job.get("dataset_name"),
                    "instruction": job["instruction"],
                    "runtime_id": runtime.id,
                    "provider": provider.id,
                    "profile_hash": job.get("profile_hash", digest(profile)),
                    "job_id": job["id"],
                    "expected_tool_commit": "1c417cc9c205f85cd01a5e5860646f1594738354",
                    **provider.lineage_metadata(profile),
                    "synthetic": synthetic,
                    "target_format": job.get("target_format", "v3.0"),
                    "run_doctor": job.get("run_doctor", True),
                    "conversion_options": options,
                    "action_corrections": [
                        {
                            "episode_index": report["episode_index"],
                            "upload_id": report["upload_id"],
                            **report["action_corrections"],
                        }
                        for report in reports
                        if report.get("action_corrections")
                    ],
                    "video_encoding": {"codec": "libx264", "pixel_format": "yuv420p"},
                },
            )
            share_data_tree(work)
            if job["processing_mode"] == "demo":
                self.save(
                    job,
                    state="DEMO_READY",
                    stage="演示对齐完成（未转换 LeRobot、未运行 Doctor）",
                    finished_at=now(),
                )
                return
            executable = executable_path(self.settings.letools_bin)
            if not executable:
                raise DomainError(
                    "未找到 letools；请按 README 在独立 Python 3.12 环境安装固定提交并配置 letools_bin"
                )
            # One allocation for this job's whole toolchain chain.
            allocation = self._open_allocation(work)
            code, _, err = self.command(
                [executable, "doctor", "environment"],
                work,
                "environment",
                allocation=allocation,
            )
            if code:
                raise DomainError(
                    "letools 环境诊断失败：" + err.read_text(errors="replace")[-1200:]
                )
            self.save(job, stage="letools 转换")
            job.update(self._conversion_parallelism())
            args = provider.conversion_args(
                executable=executable,
                source=work / "aligned",
                destination=work / "dataset",
                profile=profile,
                job=job,
            )
            code, _, err = self.command(args, work, "convert", allocation=allocation)
            if code:
                raise DomainError(
                    f"转换失败（{code}）：{err.read_text(errors='replace')[-2000:]}"
                )
            provider.patch_dataset_info(work / "dataset", profile, synthetic=synthetic)
            # Run after the provider's atomic metadata rewrite, and before the
            # dataset becomes available to previews or downstream users. This
            # also fixes restrictive modes created inside a Slurm allocation.
            share_data_tree(work)
            info = json.loads((work / "dataset/meta/info.json").read_text())
            expected_episodes = len(reports)
            expected_frames = job["alignment_report"]["frames"]
            if job.get("trim_idle_frames"):
                self.save(job, stage="检测并裁剪首尾静止帧")
                trimmed = self.trims.apply_to_conversion(job, work, allocation, info)
                expected_episodes = trimmed["episodes"]
                expected_frames = trimmed["frames"]
                self.save(
                    job,
                    trim_summary=trimmed["summary"],
                    trim_report=trimmed["report"],
                    trim_policy=trimmed["policy"],
                    episode_count=expected_episodes,
                    frame_count=expected_frames,
                )
            elif job.get("trim_static_intervals"):
                self.save(job, stage="检测并删除全程静止区间")
                trimmed = self.trims.apply_intervals_to_conversion(
                    job, work, allocation, info
                )
                expected_episodes = trimmed["episodes"]
                expected_frames = trimmed["frames"]
                self.save(
                    job,
                    interval_summary=trimmed["summary"],
                    interval_report=trimmed["report"],
                    interval_policy=trimmed["policy"],
                    episode_count=expected_episodes,
                    frame_count=expected_frames,
                )
            self.save(job, stage="深度结构验证")
            self._deep_validate(executable, work, allocation)
            info = json.loads((work / "dataset/meta/info.json").read_text())
            if (
                info.get("total_episodes") != expected_episodes
                or info.get("total_frames") != expected_frames
            ):
                raise DomainError("转换结果的 episode / 帧数与输入快照不一致")
            self.save(
                job,
                dataset_ready=True,
                dataset_path=str(work / "dataset"),
                episode_count=info.get("total_episodes"),
                frame_count=info.get("total_frames"),
            )
            if not job.get("run_doctor", True):
                self._close_allocation()
                self.save(
                    job,
                    state="CONVERTED",
                    stage="转换完成，可预览或运行 Doctor",
                    finished_at=now(),
                )
                return
            self.save(job, stage="Doctor 全量质检")
            code, output, err = self.command(
                [
                    executable,
                    "doctor",
                    "check",
                    str(work / "dataset"),
                    "--checks",
                    ",".join(DOCTOR_CHECK_IDS),
                    "--ci",
                    "--fail-on",
                    "warn",
                ],
                work,
                "doctor",
                allocation=allocation,
            )
            try:
                doctor = json.loads(output.read_text())
                if not isinstance(doctor, dict) or not doctor:
                    raise ValueError("empty report")
            except (ValueError, OSError) as exc:
                raise DomainError(
                    f"Doctor 未生成有效 JSON，属于任务错误而不是质量通过：{err.read_text(errors='replace')[-1200:]}"
                ) from exc
            self._close_allocation()
            share_data_tree(work)
            self.save(job, doctor_report=doctor)
            info = json.loads((work / "dataset/meta/info.json").read_text())
            gate = doctor_gate(doctor, info, code)
            job["quality_gate"] = gate
            job["episode_count"] = info.get("total_episodes")
            if not gate["passed"]:
                self.save(
                    job,
                    state="QUARANTINED",
                    stage="转换完成，Doctor 发现问题",
                    error=f"Doctor exit={code}, severity={gate['severity']}",
                    dataset_path=str(work / "dataset"),
                    finished_at=now(),
                )
                return
            self.save(
                job,
                state="COMPLETED" if not synthetic else "SYNTHETIC_READY",
                stage=(
                    "转换与 Doctor 质检完成"
                    if not synthetic
                    else "模拟数据转换与 Doctor 质检完成"
                ),
                dataset_path=str(work / "dataset"),
                finished_at=now(),
            )
        except InterruptedError as exc:
            self._close_allocation()
            self.save(
                job,
                state="INTERRUPTED",
                stage="Worker 已停止",
                error=str(exc),
                finished_at=now(),
            )
        except Exception as exc:
            self._close_allocation()
            self.save(
                job, state="FAILED", stage="任务失败", error=str(exc), finished_at=now()
            )
