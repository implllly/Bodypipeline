"""Convert registered LeRobot datasets between the supported physical versions."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path

from pipeline.shared.collections import dataset_name
from pipeline.shared.core import DomainError, atomic_json, digest, executable_path, now

from .datasets import DATA_READY_STATES, TARGET_FORMATS, dataset_for_job
from .merges import source_snapshot
from .quality import DOCTOR_CHECK_IDS, doctor_gate
from .recycle import dataset_lock
from .storage import (
    ROBOT_LAYOUT,
    data_directory,
    robot_type,
    share_data_tree,
    storage_path,
)

LETOOLS_COMMIT = "1c417cc9c205f85cd01a5e5860646f1594738354"


class VersionConversionRunner:
    """Reuse the serial job queue, Slurm allocation and shutdown contract."""

    def __init__(self, settings, runner):
        self.settings, self.runner, self.store = settings, runner, runner.store

    @staticmethod
    def _path_key(path):
        return hashlib.sha256(str(path).encode()).hexdigest()

    def _check_recycle(self, path):
        try:
            row = self.store.get("recycle", self._path_key(path))
        except DomainError as exc:
            if exc.code == 404:
                return
            raise
        if (row.get("operation") or {}).get("state") in {"running", "pending"}:
            raise DomainError("来源正在永久清理，请等待完成后再转换版本")
        if row.get("entries"):
            raise DomainError("来源存在回收站 Episode，请先还原或永久清理再转换版本")

    def _describe(self, job):
        path = dataset_for_job(self.settings, job)
        try:
            info = json.loads((path / "meta/info.json").read_text(encoding="utf-8"))
            if not isinstance(info, dict):
                raise ValueError("meta/info.json 必须是 JSON 对象")
            version = info.get("codebase_version")
            if version not in TARGET_FORMATS:
                raise ValueError("仅支持 LeRobot v2.1 / v3.0")
            registered = job.get("robot_type") or job.get("profile", {}).get(
                "robot_type"
            )
            detected = info.get("robot_type")
            kind = robot_type(detected or registered or "unknown")
            if detected is not None and registered and kind != registered:
                raise ValueError("数据集 robot_type 与登记记录不一致")
            fps = info.get("fps")
            episodes = info.get("total_episodes")
            frames = info.get("total_frames")
            features = info.get("features")
            if type(fps) not in (int, float) or fps <= 0:
                raise ValueError("FPS 必须是正数")
            if any(
                type(value) is not int or value <= 0 for value in (episodes, frames)
            ):
                raise ValueError("Episode / 帧数必须是正整数")
            if not isinstance(features, dict) or not features:
                raise ValueError("缺少特征定义")
            self._check_recycle(path)
            source_format = str(version)
            return {
                "dataset_path": str(path),
                "source_format": source_format,
                "target_format": "v3.0" if source_format == "v2.1" else "v2.1",
                "robot_type": kind,
                "fps": float(fps),
                "episode_count": episodes,
                "frame_count": frames,
                "synthetic": job.get("capture_mode") == "mock"
                or info.get("synthetic") is True,
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise DomainError(f"来源数据集无法进行版本转换：{exc}", 409) from exc

    def candidates(self):
        rows = []
        for job in self.store.all("job"):
            if job["state"] not in DATA_READY_STATES and not job.get("dataset_ready"):
                continue
            # An in-place trim shares the source path and is tracked on the
            # source job, so selecting it would duplicate the same dataset.
            if job.get("operation") == "manual_trim" and job.get("in_place") is True:
                continue
            row = {
                "id": job["id"],
                "source_job_id": job["id"],
                "dataset_name": job.get("dataset_name")
                or job.get("instruction")
                or job["id"],
                "robot_type": job.get("robot_type")
                or job.get("profile", {}).get("robot_type"),
                "state": job["state"],
                "operation": job.get("operation"),
                "created_at": job.get("created_at"),
            }
            try:
                row.update(self._describe(job), selectable=True)
            except (DomainError, OSError) as exc:
                row.update(selectable=False, reason=str(exc))
            rows.append(row)
        return rows

    def enqueue(
        self,
        source_job_id,
        target_format,
        name=None,
        run_doctor=True,
    ):
        if target_format not in TARGET_FORMATS:
            raise DomainError("目标格式必须为 v3.0 / v2.1", 400)
        if type(run_doctor) is not bool:
            raise DomainError("run_doctor 必须为布尔值", 400)
        tool = executable_path(self.settings.letools_bin)
        if not tool:
            raise DomainError("版本转换需要可执行的 LeTools 环境", 503)
        with (
            self.runner.lock,
            dataset_lock(
                self.store,
                self._path_key(
                    dataset_for_job(
                        self.settings,
                        self.store.get("job", source_job_id),
                    )
                ),
            ),
        ):
            source_job = self.store.get("job", source_job_id)
            source = self._describe(source_job)
            if source["source_format"] == target_format:
                raise DomainError("来源已经是目标 LeRobot 版本", 400)
            if source["target_format"] != target_format:
                raise DomainError("LeRobot 仅支持 v2.1 与 v3.0 互相转换", 400)
            source.update(source_snapshot(Path(source["dataset_path"])))
            if isinstance(name, str) and name.strip():
                output_name = dataset_name(name)
            else:
                source_name = (
                    source_job.get("dataset_name")
                    or source_job.get("instruction")
                    or source_job_id
                )
                output_name = dataset_name(f"{source_name[:1990]} · {target_format}")
            key = "version-conversion:" + digest(
                {
                    "source_job_id": source_job_id,
                    "source_signature": source["signature"],
                    "source_format": source["source_format"],
                    "target_format": target_format,
                    "name": output_name,
                    "run_doctor": run_doctor,
                    "letools": LETOOLS_COMMIT,
                }
            )
            for row in self.store.all("job"):
                if row.get("key") == key and row["state"] not in {
                    "FAILED",
                    "INTERRUPTED",
                    "BLOCKED",
                    "DELETING",
                    "DELETE_FAILED",
                    "DELETED",
                }:
                    return row
            job = {
                "id": str(uuid.uuid4()),
                "key": key,
                "operation": "version_conversion",
                "source_kind": "lerobot_version_conversion",
                "source_job_id": source_job_id,
                "source_dataset_name": source_job.get("dataset_name")
                or source_job.get("instruction")
                or source_job_id,
                "source_format": source["source_format"],
                "target_format": target_format,
                "source_snapshot": {
                    "signature": source["signature"],
                    "bytes": source["bytes"],
                },
                "source_description": {
                    key: value
                    for key, value in source.items()
                    if key not in {"signature", "bytes"}
                },
                "dataset_name": output_name,
                "instruction": "",
                "state": "QUEUED",
                "stage": "等待 LeRobot 版本转换",
                "created_at": now(),
                "error": None,
                "robot_type": source["robot_type"],
                "capture_mode": "mock" if source["synthetic"] else "version_conversion",
                "processing_mode": "real",
                "run_doctor": run_doctor,
                "source_count": source["episode_count"],
                "episode_count": source["episode_count"],
                "frame_count": source["frame_count"],
            }
            for field in ("runtime_id", "provider", "profile", "profile_hash"):
                if source_job.get(field) is not None:
                    job[field] = source_job[field]
            if self.settings.data_root is not None:
                job["data_layout"] = ROBOT_LAYOUT
            destination = storage_path(self.settings, job, "work", job["id"])
            if destination.is_relative_to(Path(source["dataset_path"])):
                raise DomainError("转换输出不能位于来源数据集内", 400)
            self.store.put("job", job["id"], job)
            return job

    def run(self, job):
        save = self.runner.save
        save(job, state="RUNNING", stage="核验 LeRobot 来源", started_at=now())
        tool = executable_path(self.settings.letools_bin)
        try:
            if not tool:
                raise DomainError("未找到 LeTools 环境", 503)
            source_job = self.store.get("job", job["source_job_id"])
            source = self._describe(source_job)
            if source["source_format"] != job["source_format"]:
                raise DomainError("来源版本与提交快照不一致")
            source_path = Path(source["dataset_path"])
            with dataset_lock(self.store, self._path_key(source_path)):
                if source != job["source_description"] or source_snapshot(
                    source_path
                ) != {
                    "signature": job["source_snapshot"]["signature"],
                    "bytes": job["source_snapshot"]["bytes"],
                }:
                    raise DomainError("来源在提交后发生变化，请重新选择并提交")
                work = data_directory(
                    self.settings, job, "work", job["id"], exist_ok=False
                )
                save(job, work_path=str(work))
                size = job["source_snapshot"]["bytes"]
                if (
                    shutil.disk_usage(work).free
                    < size + size // 4 + self.settings.min_free_bytes
                ):
                    raise DomainError("输出存储空间不足，无法创建版本转换数据集", 507)
                lineage_path = work / "version-conversion-input.json"
                atomic_json(
                    lineage_path,
                    {
                        "source_job_id": job["source_job_id"],
                        "source_dataset_name": job["source_dataset_name"],
                        "source_path": str(source_path),
                        "source_format": job["source_format"],
                        "target_format": job["target_format"],
                        "source_snapshot": job["source_snapshot"],
                        "letools_commit": LETOOLS_COMMIT,
                    },
                )
                job.update(self.runner._conversion_parallelism())
                save(
                    job,
                    convert_workers=job["convert_workers"],
                    video_workers=job["video_workers"],
                    video_codec_threads=job["video_codec_threads"],
                )
                save(job, stage="等待版本转换计算资源")
                allocation = self.runner._open_allocation(work)
                try:
                    save(job, stage="诊断 LeTools 环境")
                    code, _, err = self.runner.command(
                        [tool, "doctor", "environment"],
                        work,
                        "environment",
                        allocation=allocation,
                    )
                    if code:
                        raise DomainError(
                            "LeTools 环境诊断失败："
                            + err.read_text(errors="replace")[-1200:]
                        )
                    save(job, stage="验证 LeRobot 来源结构")
                    code, _, err = self.runner.command(
                        [tool, "validate", str(source_path)],
                        work,
                        "source-validate",
                        allocation=allocation,
                    )
                    if code:
                        raise DomainError(
                            "来源结构验证失败："
                            + err.read_text(errors="replace")[-2000:]
                        )
                    save(
                        job,
                        stage=f"转换 {job['source_format']} → {job['target_format']}",
                    )
                    output = work / "dataset"
                    code, _, err = self.runner.command(
                        [
                            tool,
                            "convert",
                            str(source_path),
                            str(output),
                            "--source-format",
                            "lerobot",
                            "--to",
                            job["target_format"],
                            "--workers",
                            str(job["convert_workers"]),
                            "--video-workers",
                            str(job["video_workers"]),
                        ],
                        work,
                        "convert",
                        allocation=allocation,
                    )
                    if code:
                        raise DomainError(
                            f"版本转换失败（{code}）："
                            + err.read_text(errors="replace")[-2000:]
                        )
                    save(job, stage="深度结构验证")
                    self.runner._deep_validate(tool, work, allocation)
                    info = json.loads(
                        (output / "meta/info.json").read_text(encoding="utf-8")
                    )
                    if (
                        info.get("codebase_version") != job["target_format"]
                        or info.get("total_episodes") != job["episode_count"]
                        or info.get("total_frames") != job["frame_count"]
                    ):
                        raise DomainError(
                            "转换结果的版本或 episode / 帧数与来源快照不一致"
                        )
                    if (
                        source_snapshot(source_path)["signature"]
                        != job["source_snapshot"]["signature"]
                    ):
                        raise DomainError("转换期间来源发生变化，结果未发布")
                    if self.runner.shutdown_requested.is_set():
                        raise InterruptedError("Worker 收到停止请求")
                    lineage = {
                        "operation": "version_conversion",
                        "job_id": job["id"],
                        "source_job_id": job["source_job_id"],
                        "source_dataset_name": job["source_dataset_name"],
                        "source_path": str(source_path),
                        "source_format": job["source_format"],
                        "target_format": job["target_format"],
                        "source_snapshot": job["source_snapshot"],
                        "letools_commit": LETOOLS_COMMIT,
                    }
                    atomic_json(
                        output / "meta/bodypipeline-version-conversion.json",
                        lineage,
                    )
                    atomic_json(work / "lineage.json", lineage)
                    share_data_tree(work)
                    if not job.get("run_doctor", True):
                        save(
                            job,
                            state="CONVERTED",
                            stage="版本转换完成，可预览或运行质检",
                            dataset_ready=True,
                            dataset_path=str(output),
                            finished_at=now(),
                        )
                        return
                    save(job, stage="Doctor 全量质检")
                    code, report_path, err = self.runner.command(
                        [
                            tool,
                            "doctor",
                            "check",
                            str(output),
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
                        doctor = json.loads(report_path.read_text())
                        if not isinstance(doctor, dict) or not doctor:
                            raise ValueError("empty report")
                    except (ValueError, OSError) as exc:
                        raise DomainError(
                            "Doctor 未生成有效 JSON，属于任务错误而不是质量通过："
                            + err.read_text(errors="replace")[-1200:]
                        ) from exc
                    gate = doctor_gate(doctor, info, code)
                    share_data_tree(work)
                    if not gate["passed"]:
                        save(
                            job,
                            state="QUARANTINED",
                            stage="版本转换完成，Doctor 发现问题",
                            error=f"Doctor exit={code}, severity={gate['severity']}",
                            dataset_ready=True,
                            dataset_path=str(output),
                            doctor_report=doctor,
                            quality_gate=gate,
                            finished_at=now(),
                        )
                        return
                    save(
                        job,
                        state="SYNTHETIC_READY"
                        if job["capture_mode"] == "mock"
                        else "COMPLETED",
                        stage="版本转换与 Doctor 质检完成",
                        dataset_ready=True,
                        dataset_path=str(output),
                        doctor_report=doctor,
                        quality_gate=gate,
                        finished_at=now(),
                    )
                finally:
                    self.runner._close_allocation()
        except InterruptedError as exc:
            self.runner._close_allocation()
            save(
                job,
                state="INTERRUPTED",
                stage="版本转换已中断",
                error=str(exc),
                finished_at=now(),
            )
        except Exception as exc:
            self.runner._close_allocation()
            save(
                job,
                state="FAILED",
                stage="版本转换失败",
                error=str(exc),
                finished_at=now(),
            )


__all__ = ["VersionConversionRunner"]
