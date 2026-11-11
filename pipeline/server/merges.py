"""Create independent datasets from compatible, server-registered results."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import stat
import uuid
from contextlib import ExitStack
from pathlib import Path

from pipeline.shared.collections import dataset_name
from pipeline.shared.core import DomainError, atomic_json, digest, executable_path, now

from .datasets import DATA_READY_STATES, TARGET_FORMATS, dataset_for_job
from .recycle import dataset_lock
from .storage import (
    ROBOT_LAYOUT,
    data_directory,
    robot_type,
    share_data_tree,
    storage_path,
)


def source_snapshot(root: Path) -> dict:
    """Fingerprint file identities without reading large video payloads or links."""
    entries, size = [], 0
    if any(p.is_symlink() for p in [root, *root.parents]):
        raise DomainError("数据集来源不能包含符号链接", 400)

    def walk(directory):
        nonlocal size
        for path in sorted(directory.iterdir()):
            entry = path.lstat()
            if stat.S_ISDIR(entry.st_mode):
                walk(path)
            elif stat.S_ISREG(entry.st_mode):
                entries.append(
                    (
                        str(path.relative_to(root)),
                        entry.st_ino,
                        entry.st_size,
                        entry.st_mtime_ns,
                    )
                )
                size += entry.st_size
            else:
                raise DomainError("数据集来源含符号链接或特殊文件：" + str(path), 400)

    try:
        walk(root)
    except OSError as exc:
        raise DomainError(f"来源文件无法读取或发生变化：{exc}", 409) from exc
    return {"signature": digest(entries), "bytes": size}


class MergeRunner:
    """Share the existing serial job queue, shutdown handling and Slurm runner."""

    def __init__(self, settings, runner):
        self.settings, self.runner, self.store = settings, runner, runner.store

    @staticmethod
    def _key(path):
        return hashlib.sha256(str(path).encode()).hexdigest()

    def _check_recycle(self, path):
        try:
            row = self.store.get("recycle", self._key(path))
        except DomainError as exc:
            if exc.code == 404:
                return
            raise
        if (row.get("operation") or {}).get("state") in {"running", "pending"}:
            raise DomainError("来源正在永久清理，请等待完成后再合并")
        if row.get("entries"):
            raise DomainError("来源存在回收站 Episode，请先还原或永久清理再合并")

    def _describe(self, job):
        path = dataset_for_job(self.settings, job)
        try:
            info = json.loads((path / "meta/info.json").read_text())
            version, fps = info["codebase_version"], info["fps"]
            episodes, frames = info["total_episodes"], info["total_frames"]
            kind = robot_type(info["robot_type"])
            if kind != (
                job.get("robot_type") or job.get("profile", {}).get("robot_type", kind)
            ):
                raise ValueError("数据集 robot_type 与登记记录不一致")
            if version not in TARGET_FORMATS:
                raise ValueError("仅支持 LeRobot v2.1 / v3.0")
            if type(fps) not in (int, float) or not math.isfinite(fps) or fps <= 0:
                raise ValueError("FPS 必须是正数")
            if any(type(n) is not int or n <= 0 for n in (episodes, frames)):
                raise ValueError("Episode / 帧数必须是正整数")
            features = copy.deepcopy(info["features"])
            if not isinstance(features, dict) or not features:
                raise ValueError("缺少特征定义")
            for feature in features.values():
                if not isinstance(feature, dict):
                    raise ValueError("特征定义无效")
                # Match the pinned LeTools merger's layout normalization.
                if feature.get("dtype") != "video":
                    feature.pop("fps", None)
            splits = info.get("splits", {"train": f"0:{episodes}"})
            if (
                not isinstance(splits, dict)
                or len(splits) != 1
                or next(iter(splits.values())) != f"0:{episodes}"
            ):
                raise ValueError("合并要求只有一个覆盖全部 Episode 的 split")
            synthetic = (
                job.get("capture_mode") == "mock" or info.get("synthetic") is True
            )
            self._check_recycle(path)
            return {
                "dataset_path": str(path),
                "target_format": version,
                "robot_type": kind,
                "fps": fps,
                "episode_count": episodes,
                "frame_count": frames,
                "synthetic": synthetic,
                "compatibility_key": digest(
                    [version, kind, float(fps), features, list(splits), synthetic]
                ),
            }
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DomainError(f"来源元数据无法合并：{exc}", 409) from exc

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

    def _sources(self, ids):
        sources = []
        for jid in ids:
            row = self.store.get("job", jid)
            sources.append(
                {
                    "job_id": jid,
                    "dataset_name": row.get("dataset_name") or jid,
                    **self._describe(row),
                }
            )
        paths = [Path(s["dataset_path"]) for s in sources]
        for index, path in enumerate(paths):
            if any(
                path == other
                or path.is_relative_to(other)
                or other.is_relative_to(path)
                for other in paths[:index]
            ):
                raise DomainError("不能重复选择同一路径或互相嵌套的数据集", 400)
        first = sources[0]
        for source in sources[1:]:
            if source["compatibility_key"] != first["compatibility_key"]:
                raise DomainError(
                    "来源不兼容：版本、机器人类型、FPS、特征结构、split 和模拟标识必须一致",
                    400,
                )
        return sources

    def enqueue(self, source_job_ids, name):
        name = dataset_name(name)
        if not 2 <= len(source_job_ids) <= 100 or len(set(source_job_ids)) != len(
            source_job_ids
        ):
            raise DomainError("请选择 2–100 个不重复的数据集", 400)
        tool = executable_path(self.settings.letools_bin)
        if not tool or not Path(tool).with_name("python").is_file():
            raise DomainError("合并需要可执行的 LeTools Python 环境", 503)
        with self.runner.lock, ExitStack() as locks:
            sources = self._sources(source_job_ids)
            for path in sorted(s["dataset_path"] for s in sources):
                locks.enter_context(dataset_lock(self.store, self._key(path)))
            # Snapshot again under locks, closing the cleanup race above.
            sources = self._sources(source_job_ids)
            for source in sources:
                source.update(source_snapshot(Path(source["dataset_path"])))
            key = "merge:" + digest(
                [name, [(s["job_id"], s["signature"]) for s in sources]]
            )
            for row in self.store.all("job"):
                if row.get("key") == key and row["state"] in {"QUEUED", "RUNNING"}:
                    return row
            job = {
                "id": str(uuid.uuid4()),
                "key": key,
                "operation": "merge",
                "source_kind": "dataset_merge",
                "source_job_ids": source_job_ids,
                "merge_sources": sources,
                "merge_source_count": len(sources),
                "dataset_name": name,
                "instruction": "",
                "state": "QUEUED",
                "stage": "等待合并",
                "created_at": now(),
                "error": None,
                "target_format": sources[0]["target_format"],
                "robot_type": sources[0]["robot_type"],
                "capture_mode": "mock" if sources[0]["synthetic"] else "merged",
                "processing_mode": "real",
                "run_doctor": False,
                "source_count": sum(s["episode_count"] for s in sources),
                "frame_count": sum(s["frame_count"] for s in sources),
            }
            records = [self.store.get("job", jid) for jid in source_job_ids]
            # Preserve common, server-owned analysis context when every source
            # agrees; never manufacture a Runtime from a dataset's robot label.
            for field in ("runtime_id", "provider", "profile", "profile_hash"):
                value = records[0].get(field)
                if value is not None and all(r.get(field) == value for r in records):
                    job[field] = value
            if self.settings.data_root is not None:
                job["data_layout"] = ROBOT_LAYOUT
            destination = storage_path(self.settings, job, "work", job["id"])
            if any(
                destination.is_relative_to(Path(s["dataset_path"])) for s in sources
            ):
                raise DomainError("合并输出不能位于来源数据集内", 400)
            self.store.put("job", job["id"], job)
            return job

    def run(self, job):
        save = self.runner.save
        save(job, state="RUNNING", stage="核验合并来源", started_at=now())
        try:
            with ExitStack() as locks:
                for path in sorted(s["dataset_path"] for s in job["merge_sources"]):
                    locks.enter_context(dataset_lock(self.store, self._key(path)))
                current = self._sources(job["source_job_ids"])
                for original, source in zip(job["merge_sources"], current):
                    if source != {
                        k: v
                        for k, v in original.items()
                        if k not in {"signature", "bytes"}
                    } or source_snapshot(Path(source["dataset_path"])) != {
                        k: original[k] for k in ("signature", "bytes")
                    }:
                        raise DomainError("来源在提交后发生变化，请重新选择并提交合并")
                destination = storage_path(self.settings, job, "work", job["id"])
                if any(
                    destination.is_relative_to(Path(s["dataset_path"])) for s in current
                ):
                    raise DomainError("合并输出不能位于来源数据集内", 400)
                work = data_directory(
                    self.settings, job, "work", job["id"], exist_ok=False
                )
                save(job, work_path=str(work))
                size = sum(s["bytes"] for s in job["merge_sources"])
                if (
                    shutil.disk_usage(work).free
                    < size + size // 4 + self.settings.min_free_bytes
                ):
                    raise DomainError("输出存储空间不足，无法创建独立合并数据集", 507)
                pending = work / "pending-dataset"
                spec = work / "merge-inputs.json"
                atomic_json(
                    spec, {"sources": job["merge_sources"], "destination": str(pending)}
                )
                share_data_tree(work)
                save(job, stage="等待合并计算资源")
                allocation = self.runner._open_allocation(work)
                tool = executable_path(self.settings.letools_bin)
                if not tool:
                    raise DomainError("未找到 LeTools 环境", 503)
                save(job, stage="合并数据、重编号与深度结构验证")
                code, report, err = self.runner.command(
                    [
                        str(Path(tool).with_name("python")),
                        str(Path(__file__).with_name("merge_worker.py")),
                        str(spec),
                    ],
                    work,
                    "merge",
                    allocation=allocation,
                )
                if code:
                    raise DomainError(
                        "合并失败：" + err.read_text(errors="replace")[-2000:]
                    )
                result = json.loads(report.read_text())
                info = json.loads((pending / "meta/info.json").read_text())
                if (
                    info.get("total_episodes") != job["source_count"]
                    or info.get("total_frames") != job["frame_count"]
                    or info.get("codebase_version") != job["target_format"]
                    or result.get("validated") is not True
                ):
                    raise DomainError("合并结果的数量、版本或结构验证与来源快照不一致")
                for source in job["merge_sources"]:
                    if (
                        source_snapshot(Path(source["dataset_path"]))["signature"]
                        != source["signature"]
                    ):
                        raise DomainError("合并期间来源发生变化，结果未发布")
                if self.runner.shutdown_requested.is_set():
                    raise InterruptedError("Worker 收到停止请求")
                save(job, stage="保存来源记录与共享数据权限")
                offset = 0
                lineage = []
                for source in job["merge_sources"]:
                    lineage.append(
                        {
                            **source,
                            "episode_start": offset,
                            "episode_end": offset + source["episode_count"],
                        }
                    )
                    offset += source["episode_count"]
                atomic_json(
                    pending / "meta/bodypipeline-merge.json",
                    {
                        "job_id": job["id"],
                        "dataset_name": job["dataset_name"],
                        "synthetic": job["capture_mode"] == "mock",
                        "sources": lineage,
                    },
                )
                if job["capture_mode"] == "mock":
                    info["synthetic"] = True
                    atomic_json(pending / "meta/info.json", info)
                atomic_json(
                    work / "lineage.json",
                    {"operation": "merge", "job_id": job["id"], "sources": lineage},
                )
                share_data_tree(work)
                destination = work / "dataset"
                os.replace(pending, destination)
                save(
                    job,
                    state="CONVERTED",
                    stage="合并完成，可预览或运行质检",
                    dataset_ready=True,
                    dataset_path=str(destination),
                    episode_count=offset,
                    merge_report=result,
                    finished_at=now(),
                )
        except InterruptedError as exc:
            save(
                job,
                state="INTERRUPTED",
                stage="合并已中断",
                error=str(exc),
                finished_at=now(),
            )
        except Exception as exc:
            save(
                job, state="FAILED", stage="合并失败", error=str(exc), finished_at=now()
            )
        finally:
            self.runner._close_allocation()
