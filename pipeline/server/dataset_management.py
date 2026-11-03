"""Registered dataset deletion with durable plans and independent source/results."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from contextlib import ExitStack
from pathlib import Path

from pipeline.shared.collections import collection_metadata, group_uploads
from pipeline.shared.core import DomainError, fsync_dir, now, safe_relative

from .datasets import _dataset_roots, dataset_for_job
from .recycle import dataset_lock
from .storage import storage_path

DELETION_STATES = {"DELETING", "DELETE_FAILED", "DELETED"}
ACTIVE = {"QUEUED", "RUNNING"}


def overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


class DatasetManager:
    def __init__(self, settings, runner, viewer):
        self.settings, self.runner, self.viewer = settings, runner, viewer
        self.ingest, self.store = runner.ingest, runner.store

    def collections(self, uploads=None):
        rows = []
        for row in self.store.all("upload") if uploads is None else uploads:
            if row["state"] == "DELETED":
                continue
            if not row.get("collection_id"):
                try:
                    row = {
                        **row,
                        **collection_metadata(
                            self.ingest.manifest(row), row["target"], row["prefix"]
                        ),
                    }
                except (DomainError, OSError, ValueError, KeyError):
                    row = {**row, "collection_id": "unavailable-" + row["id"]}
            rows.append(row)
        return group_uploads(rows)

    def _safety_context(self):
        roots = [root.resolve() for root in _dataset_roots(self.settings)]
        protected = [*roots, self.store.path.resolve()]
        for area in ("raw", "work", "incoming", "analyses", "annotations", "reports"):
            protected.append(self.settings.root.resolve() / area)
        if self.settings.data_root is not None:
            if not self.settings.data_root.is_dir():
                raise DomainError("数据根目录不可用，请检查存储挂载", 503)
            for runtime in self.runner.catalog.all():
                child = self.settings.data_root.resolve() / runtime.robot_type
                protected.extend(
                    [
                        child,
                        *(
                            child / area
                            for area in (
                                "raw",
                                "work",
                                "incoming",
                                "analyses",
                                "annotations",
                            )
                        ),
                    ]
                )
        allowed = [
            self.settings.root.resolve() / area
            for area in ("raw", "work", "incoming", "analyses")
        ]
        allowed.extend(root.resolve() for root in self.settings.import_roots)
        if self.settings.data_root is not None:
            allowed.append(self.settings.data_root.resolve())
        return allowed, protected, self.store.path.resolve()

    def _safe(self, path: Path, context=None) -> Path:
        # Check the original spelling before resolve: resolving first hides links.
        if not path.is_absolute() or ".." in path.parts:
            raise DomainError("删除路径必须为受管理的绝对路径", 403)
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise DomainError("删除路径或其父目录包含符号链接，拒绝删除", 403)
        path = path.resolve()
        allowed, protected, state_root = (
            self._safety_context() if context is None else context
        )
        if (
            not any(path.is_relative_to(root) and path != root for root in allowed)
            or any(root == path or root.is_relative_to(path) for root in protected)
            or path.is_relative_to(state_root)
        ):
            raise DomainError("不能删除存储根目录、服务状态或不在允许范围内的数据", 403)
        return path

    def _raw_paths(self, row):
        if row["state"] in DELETION_STATES and row.get("deletion_id"):
            return [Path(path) for path in row["deletion_paths"]]
        if not row.get("imported"):
            return [self.ingest.root(row)]
        raw = Path(row["raw_path"])
        manifest = self.ingest.manifest(row)
        if row.get("manifest_inline"):
            # HDF5 imports register individual files in a possibly shared directory.
            return [raw]
        paths = [raw / safe_relative(spec["path"]) for spec in manifest["files"]]
        paths.append(Path(row["manifest_path"]))
        return paths

    def check_import(self, raw_path):
        path = Path(raw_path).expanduser().resolve()
        for operation in self.store.all("dataset_deletion"):
            if operation["state"] == "COMPLETED":
                continue
            if any(
                overlaps(path, Path(t[field]))
                for t in operation["targets"]
                for field in ("path", "trash")
            ):
                raise DomainError(
                    "该路径存在未完成的删除任务，请等待完成或重试删除", 409
                )

    def _selection(self, kind, identifier, groups=None, job=None):
        if kind == "raw":
            group = next(
                (
                    c
                    for c in (self.collections() if groups is None else groups)
                    if c["id"] == identifier
                ),
                None,
            )
            if group is None:
                raise DomainError("原始数据集不存在", 404)
            rows = group["uploads"]
            if any(r["state"] != "COMMITTED" for r in rows):
                raise DomainError("原始数据仍在上传、提交或删除中，请等待完成", 409)
            paths = [p for r in rows for p in self._raw_paths(r)]
            return (
                group["dataset_name"],
                group["robot_type"],
                [("upload", r) for r in rows],
                paths,
            )
        row = job if job is not None else self.store.get("job", identifier)
        path = dataset_for_job(self.settings, row)
        # Also validate the recorded spelling, since dataset_for_job resolves it.
        if row.get("dataset_path"):
            self._safe(Path(row["dataset_path"]))
        return (
            row.get("dataset_name") or row.get("instruction") or identifier,
            row.get("robot_type") or row.get("profile", {}).get("robot_type"),
            [("job", row)],
            [path],
        )

    def _snapshot(self):
        # Read each state file once per management request, rather than once per
        # candidate. Resolve references once as well (shared storage can be slow).
        snapshot = {
            kind: self.store.all(kind)
            for kind in (
                "upload",
                "job",
                "check",
                "analysis",
                "annotation",
                "dataset_deletion",
                "recycle",
            )
        }
        snapshot["reference_owners"] = {}
        snapshot["descendant_owners"] = {}
        snapshot["reference_errors"] = {}
        for kind in ("upload", "job"):
            for row in snapshot[kind]:
                if row["state"] == "DELETED":
                    continue
                try:
                    if kind == "upload":
                        paths = self._raw_paths(row)
                    else:
                        paths = [
                            storage_path(self.settings, row, "work", row["id"])
                            / "dataset"
                        ]
                        if row.get("dataset_path"):
                            paths.append(Path(row["dataset_path"]))
                    owner = (kind, row["id"])
                    for path in paths:
                        path = path.resolve()
                        snapshot["reference_owners"].setdefault(path, set()).add(owner)
                        for parent in path.parents:
                            snapshot["descendant_owners"].setdefault(parent, set()).add(owner)
                except (DomainError, OSError, ValueError, KeyError) as exc:
                    snapshot["reference_errors"][(kind, row["id"])] = str(exc)
        return snapshot

    def _validate(self, records, paths, snapshot=None):
        snapshot = self._snapshot() if snapshot is None else snapshot
        context = self._safety_context()
        candidates = {self._safe(p, context) for p in paths}
        paths = sorted(
            (p for p in candidates if not any(parent in candidates for parent in p.parents)),
            key=str,
        )
        selected = {(kind, row["id"]) for kind, row in records}
        upload_ids = {row["id"] for kind, row in records if kind == "upload"}
        job_ids = {row["id"] for kind, row in records if kind == "job"}
        jobs = snapshot["job"]
        for job in jobs:
            refs = set(job.get("upload_ids") or [job.get("upload_id")])
            sources = set(job.get("source_job_ids") or [job.get("source_job_id")])
            if job["state"] in ACTIVE and (
                job["id"] in job_ids or refs & upload_ids or sources & job_ids
            ):
                raise DomainError(
                    "数据集正在被转换、合并、版本转换或首尾裁剪任务使用，请等待任务完成", 409
                )
        for owner, error in snapshot["reference_errors"].items():
            if owner not in selected:
                raise DomainError(
                    "无法核实其他数据集的文件引用，暂不允许删除：" + error, 409
                )
        # Index ancestors once per snapshot instead of comparing every raw
        # episode with every other episode on each management poll.
        for path in paths:
            owners = set(snapshot["descendant_owners"].get(path, ()))
            for parent in (path, *path.parents):
                owners.update(snapshot["reference_owners"].get(parent, ()))
            if owners - selected:
                raise DomainError(
                    "删除范围与另一个已登记数据集重叠，请先核对重复登记", 409
                )
        for kind in ("check", "analysis", "annotation"):
            for row in snapshot[kind]:
                ref = row.get("dataset_path")
                if row["state"] in ACTIVE and (
                    row.get("job_id") in job_ids
                    or (ref and any(overlaps(p, Path(ref).resolve()) for p in paths))
                ):
                    raise DomainError("数据集正在质检、分析或自动标注，请等待任务完成", 409)
        for deletion in snapshot["dataset_deletion"]:
            if deletion["state"] == "COMPLETED":
                continue
            if any(
                overlaps(p, Path(t["path"])) for p in paths for t in deletion["targets"]
            ):
                raise DomainError("该范围已有删除任务，请等待完成或重试原任务", 409)
        recycle_by_key = {r["id"]: r for r in snapshot["recycle"]}
        for path in paths:
            key = hashlib.sha256(str(path).encode()).hexdigest()
            recycle = recycle_by_key.get(key)
            if recycle:
                if (recycle.get("operation") or {}).get("state") in {
                    "pending",
                    "running",
                }:
                    raise DomainError("数据集正在清理 Episode，请等待完成", 409)
        return paths

    def listing(self):
        results = []
        snapshot = self._snapshot()
        groups = self.collections(snapshot["upload"])
        selections = [("raw", c["id"], c) for c in groups]
        for job in snapshot["job"]:
            if job["state"] == "DELETED":
                continue
            try:
                dataset_for_job(self.settings, job)
            except DomainError:
                if job["state"] not in DELETION_STATES:
                    continue
            selections.append(("result", job["id"], job))
        for kind, identifier, row in selections:
            states = (
                [r["state"] for r in row["uploads"]]
                if kind == "raw"
                else [row["state"]]
            )
            deletion_id = next(
                (
                    r.get("deletion_id")
                    for r in row.get("uploads", [row])
                    if r.get("deletion_id")
                ),
                None,
            )
            item = {
                "kind": kind,
                "id": identifier,
                "dataset_name": row.get("dataset_name")
                or row.get("instruction")
                or identifier,
                "robot_type": row.get("robot_type")
                or row.get("profile", {}).get("robot_type"),
                "episode_count": row.get("episode_count") or row.get("source_count"),
                "total_bytes": row.get("total_bytes"),
                "target_format": row.get("target_format"),
                "state": "DELETE_FAILED"
                if "DELETE_FAILED" in states
                else "DELETING"
                if "DELETING" in states
                else "READY",
                "deletion_id": deletion_id,
                "deletable": False,
                "paths": [],
            }
            try:
                _, _, records, paths = self._selection(
                    kind,
                    identifier,
                    groups=groups,
                    job=row if kind == "result" else None,
                )
                item["paths"] = [str(p) for p in paths]
                self._validate(records, paths, snapshot)
                item["deletable"] = True
                item["reason"] = ""
            except (DomainError, OSError, ValueError, KeyError) as exc:
                item["reason"] = str(exc)
                if item["state"] == "READY":
                    item["state"] = "UNAVAILABLE"
            results.append(item)
        # The queue's inode/path inventory stays in private state; polling only
        # needs its display fields, even for collections with thousands of files.
        fields = (
            "id",
            "state",
            "created_at",
            "finished_at",
            "dataset_name",
            "robot_type",
            "kind",
            "error",
        )
        return {
            "datasets": results,
            "deletions": [
                {key: op.get(key) for key in fields}
                for op in snapshot["dataset_deletion"]
            ],
        }

    def _mark(self, operation, state):
        for spec in operation["records"]:
            row = self.store.get(spec["kind"], spec["id"])
            row.update(state=state, deletion_id=operation["id"], updated_at=now())
            if spec["kind"] == "upload":
                row["deletion_paths"] = spec["paths"]
            if spec["kind"] == "job":
                row.update(
                    dataset_ready=False,
                    dataset_available=False,
                    stage="数据集已删除" if state == "DELETED" else "删除数据集",
                )
            self.store.put(spec["kind"], row["id"], row)

    def enqueue(self, kind, identifier, confirmation):
        with self.runner.lock, self.ingest.lock:
            name, robot, records, paths = self._selection(kind, identifier)
            if confirmation != name:
                raise DomainError("确认名称与当前数据集名称不一致，请刷新后重试", 400)
            paths = self._validate(records, paths)
            with ExitStack() as stack:
                # Raw collections may contain thousands of individual files;
                # admission is covered by ingest/runner locks and tombstones.
                # Only LeRobot datasets need the cross-process Viewer lock.
                lock_paths = paths if kind == "result" else []
                for path in lock_paths:
                    stack.enter_context(
                        dataset_lock(
                            self.store, hashlib.sha256(str(path).encode()).hexdigest()
                        )
                    )
                # The Viewer has a separate process. A recycle operation may
                # have been queued between validation and acquiring its lock.
                self._validate(records, paths)
                identifier = uuid.uuid4().hex
                targets = []
                for index, path in enumerate(paths):
                    # Missing data/mounts must never count as successful deletion.
                    entry, parent = path.stat(), path.parent.stat()
                    if entry.st_dev != parent.st_dev:
                        raise DomainError("数据集本身是挂载点，不能直接删除", 409)
                    targets.append(
                        {
                            "path": str(path),
                            "trash": str(
                                path.with_name(
                                    f".bodypipeline-delete-{identifier}-{index}"
                                )
                            ),
                            "dev": entry.st_dev,
                            "ino": entry.st_ino,
                            "parent_ino": parent.st_ino,
                        }
                    )
                operation = {
                    "id": identifier,
                    "state": "QUEUED",
                    "created_at": now(),
                    "dataset_name": name,
                    "robot_type": robot,
                    "kind": kind,
                    "lock_paths": [str(path) for path in lock_paths],
                    "error": None,
                    "targets": targets,
                    "records": [
                        {
                            "kind": k,
                            "id": r["id"],
                            **(
                                {"paths": [str(p) for p in self._raw_paths(r)]}
                                if k == "upload"
                                else {}
                            ),
                        }
                        for k, r in records
                    ],
                }
                # Persist intent before hiding records; the worker replays this after a restart.
                self.store.put("dataset_deletion", identifier, operation)
                self._mark(operation, "DELETING")
                self._revoke(operation)
                return operation

    def _revoke(self, operation):
        paths = [Path(t["path"]) for t in operation["targets"]]
        for preview in self.store.all("preview"):
            if any(
                overlaps(path, Path(preview["dataset_path"]).resolve())
                for path in paths
            ):
                self.viewer.close(preview["job_id"])

    def retry(self, identifier):
        with self.runner.lock, self.ingest.lock:
            operation = self.store.get("dataset_deletion", identifier)
            if operation["state"] != "FAILED":
                raise DomainError("只有失败的删除任务可以重试", 409)
            operation.update(state="QUEUED", error=None, updated_at=now())
            self.store.put("dataset_deletion", identifier, operation)
            self._mark(operation, "DELETING")
            return operation

    def recover(self):
        for operation in self.store.all("dataset_deletion"):
            if operation["state"] in {"QUEUED", "RUNNING", "FAILED"}:
                self._mark(
                    operation,
                    "DELETE_FAILED" if operation["state"] == "FAILED" else "DELETING",
                )
                self._revoke(operation)

    def tick(self):
        pending = [
            r
            for r in self.store.all("dataset_deletion")
            if r["state"] in {"QUEUED", "RUNNING"}
        ]
        if not pending:
            return False
        operation = pending[-1]
        try:
            with ExitStack() as stack:
                with self.runner.lock, self.ingest.lock:
                    for lock_path in operation["lock_paths"]:
                        path = self._safe(Path(lock_path))
                        stack.enter_context(
                            dataset_lock(
                                self.store,
                                hashlib.sha256(str(path).encode()).hexdigest(),
                            )
                        )
                    self._mark(operation, "DELETING")
                    self._revoke(operation)
                    operation.update(state="RUNNING", updated_at=now())
                    self.store.put("dataset_deletion", operation["id"], operation)
                # Keep dataset locks during I/O; tombstones reject new consumers.
                for target in operation["targets"]:
                    path, trash = (
                        self._safe(Path(target["path"])),
                        self._safe(Path(target["trash"])),
                    )
                    parent = path.parent.stat()
                    if (parent.st_dev, parent.st_ino) != (
                        target["dev"],
                        target["parent_ino"],
                    ):
                        raise DomainError(
                            "数据目录或存储挂载发生变化，拒绝继续删除", 409
                        )
                    for candidate in (path, trash):
                        if candidate.exists():
                            entry = candidate.stat()
                            if (entry.st_dev, entry.st_ino) != (
                                target["dev"],
                                target["ino"],
                            ):
                                raise DomainError(
                                    "删除目标已被替换，拒绝删除；请管理员检查", 409
                                )
                    if path.exists():
                        if trash.exists():
                            raise DomainError("待删除临时目录冲突", 409)
                        os.rename(path, trash)
                        fsync_dir(path.parent)
                    if trash.is_dir():
                        shutil.rmtree(trash)
                    elif trash.exists():
                        trash.unlink()
                    fsync_dir(path.parent)
                self._mark(operation, "DELETED")
                operation.update(state="COMPLETED", finished_at=now(), error=None)
        except Exception as exc:
            operation.update(state="FAILED", error=str(exc), updated_at=now())
            self._mark(operation, "DELETE_FAILED")
        self.store.put("dataset_deletion", operation["id"], operation)
        return True
