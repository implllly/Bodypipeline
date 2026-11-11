from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

from pipeline.shared.collections import collection_metadata, dataset_name
from pipeline.shared.core import (
    DomainError,
    Store,
    atomic_json,
    confined,
    digest,
    file_hash,
    fsync_dir,
    now,
    safe_relative,
)

from .providers import RobotRuntime, RuntimeCatalog
from .providers.raw_contract import raw_file_contract
from .storage import (
    ROBOT_LAYOUT,
    data_directory,
    prepare_storage,
    robot_type,
    share_data_path,
    share_data_tree,
    storage_path,
)


class IngestService:
    def __init__(self, settings):
        self.settings = settings
        self.store = Store(settings.root / "state", legacy_name="server.sqlite")
        self.lock = threading.RLock()
        self.upload_locks = {}
        self.upload_locks_guard = threading.Lock()
        self.catalog = RuntimeCatalog(settings)
        for name in ("incoming", "raw", "work", "reports"):
            (settings.root / name).mkdir(parents=True, exist_ok=True)
        if settings.data_root is not None and not settings.data_root.is_dir():
            raise DomainError("数据根目录不存在，请先创建并挂载 data_root", 503)
        self._manifest_cache = {}

    @contextmanager
    def _upload_lock(self, uid):
        # Per-upload serialization keeps one stalled storage operation from
        # blocking unrelated manifests and uploads behind the global lock.
        with self.upload_locks_guard:
            lock = self.upload_locks.setdefault(uid, threading.RLock())
        with lock:
            yield

    def validate_manifest(
        self,
        m,
        identity=None,
        enforce_identity=True,
        runtime: RobotRuntime | None = None,
    ):
        if (
            not isinstance(m, dict)
            or m.get("manifest_version") != 1
            or not m.get("complete")
        ):
            raise DomainError("无效或未完成的 Manifest", 400)
        identity = identity or {
            "robot_id": self.settings.robot_id,
            "project_id": self.settings.project_id,
        }
        if enforce_identity and (
            m.get("robot_id") != identity["robot_id"]
            or m.get("project_id") != identity["project_id"]
        ):
            raise DomainError("设备身份不属于授权机器人或项目", 403)
        if not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", str(m.get("session_id", ""))):
            raise DomainError("非法 session_id", 400)
        if m.get("control_mode") != "teleoperation":
            raise DomainError("此版本仅支持主从臂遥操作", 400)
        if m.get("capture_mode") not in {"mock", "ros2", "real"}:
            raise DomainError("未声明采集模式", 400)
        if not isinstance(m.get("instruction"), str) or len(m["instruction"]) > 2000:
            raise DomainError("instruction 必须为字符串且不超过 2000 字符", 400)
        dataset_name(m.get("dataset_name") or m.get("instruction"))
        if not isinstance(m.get("profile"), dict) or digest(m["profile"]) != m.get(
            "profile_hash"
        ):
            raise DomainError("Profile 摘要不一致", 400)
        robot_type(m["profile"].get("robot_type"))
        if runtime is not None:
            self.catalog.validate_capture(runtime, m)
        elif not isinstance(m.get("raw_format"), str) or not m["raw_format"]:
            raise DomainError("Manifest 缺少 raw_format", 400)
        files = m.get("files")
        if (
            not isinstance(files, list)
            or not 1 <= len(files) <= self.settings.max_files
        ):
            raise DomainError("无效文件清单", 400)
        names, total = set(), 0
        provider = self.catalog.provider(runtime) if runtime is not None else None
        contract = raw_file_contract(m.get("raw_format"), provider)
        if contract.single_file and len(files) != 1:
            raise DomainError("HDF5 采集包必须且只能包含一个文件", 400)
        for f in files:
            if not isinstance(f, dict) or set(f) != {"path", "size_bytes", "sha256"}:
                raise DomainError("无效文件字段", 400)
            path = safe_relative(f["path"])
            contract.validate_path(path)
            if path in names:
                raise DomainError("文件不能重复", 400)
            names.add(path)
            if (
                type(f["size_bytes"]) is not int
                or f["size_bytes"] < 0
                or (contract.single_file and f["size_bytes"] == 0)
                or not re.fullmatch(r"[0-9a-f]{64}", str(f["sha256"]))
            ):
                raise DomainError("无效文件大小或摘要", 400)
            total += f["size_bytes"]
        # Reject file/directory prefix collisions before any writes.
        for name in names:
            if any(str(p) in names for p in Path(name).parents if str(p) != "."):
                raise DomainError("文件路径与目录冲突", 400)
        if total > self.settings.max_upload_bytes:
            raise DomainError("超过单次采集容量限制", 413)
        return total

    def create(
        self, m, target, prefix, identity=None, runtime: RobotRuntime | None = None
    ):
        total = self.validate_manifest(m, identity, runtime=runtime)
        safe_relative(prefix, True)
        if identity and target not in identity.get(
            "targets", list(self.settings.targets)
        ):
            raise DomainError("设备无权使用该存储目标", 403)
        if target not in self.settings.targets:
            raise DomainError("远端存储目标不可用", 400)
        key = digest(
            {
                "robot": m["robot_id"],
                "project": m["project_id"],
                "session": m["session_id"],
            }
        )[:32]
        manifest_hash = digest(m)
        with self._upload_lock(key):
            try:
                row = self.store.get("upload", key)
            except DomainError as exc:
                if exc.code != 404:
                    raise
            else:
                if row["state"] in {"DELETING", "DELETE_FAILED", "DELETED"}:
                    raise DomainError("该采集已删除或正在删除；请使用新的采集 ID", 409)
                if (
                    row["manifest_hash"] != manifest_hash
                    or row["target"] != target
                    or row["prefix"] != prefix
                    or (
                        runtime is not None
                        and row.get("runtime_id") not in (None, runtime.id)
                    )
                ):
                    raise DomainError("同一采集 ID 已绑定其他数据、Runtime 或存储目标")
                return row
            reserved = sum(
                r["total_bytes"]
                for r in self.store.all("upload")
                if r["state"] in {"UPLOADING", "COMMITTING"}
            )
            storage = {"robot_type": robot_type(m["profile"].get("robot_type"))}
            if runtime is not None:
                storage.update(
                    runtime_id=runtime.id,
                    provider=runtime.provider,
                    profile_hash=runtime.profile_hash,
                )
            if self.settings.data_root is not None:
                storage["data_layout"] = ROBOT_LAYOUT
            prepare_storage(self.settings, storage)
            incoming = storage_path(self.settings, storage, "incoming")
            if (
                shutil.disk_usage(incoming).free - reserved - total
                < self.settings.min_free_bytes
            ):
                raise DomainError("服务器可用空间不足（含待上传配额预留）", 507)
            staging = storage_path(self.settings, storage, "incoming", key)
            data_directory(self.settings, storage, "incoming", f"{key}/data")
            atomic_json(staging / "manifest.json", m)
            share_data_path(staging / "manifest.json")
            row = {
                **storage,
                **collection_metadata(m, target, prefix),
                "collection_layout": True,
                "id": key,
                "state": "UPLOADING",
                "manifest_hash": manifest_hash,
                "session_id": m["session_id"],
                "robot_id": m["robot_id"],
                "project_id": m["project_id"],
                "target": target,
                "prefix": prefix,
                "created_at": now(),
                "total_bytes": total,
                "file_count": len(m["files"]),
                "instruction": m["instruction"],
                "capture_mode": m["capture_mode"],
            }
            self.store.put("upload", key, row)
            return row

    def register_import(
        self, manifest, manifest_path, raw_path, runtime=None, *, inline=False
    ):
        """Register an existing on-disk capture without copying or rewriting it.

        ``inline`` keeps the manifest in the store instead of reading it from the
        source tree. An upstream HDF5 episode has no manifest on disk, and the
        import must not write anything into the imported directory.
        """
        total = self.validate_manifest(
            manifest, enforce_identity=False, runtime=runtime
        )
        manifest_hash = digest(manifest)
        key = digest(
            {
                "kind": "raw-import",
                "manifest_hash": manifest_hash,
                "raw_path": str(Path(raw_path).resolve()),
            }
        )[:32]
        with self._upload_lock(key):
            try:
                row = self.store.get("upload", key)
            except DomainError as exc:
                if exc.code != 404:
                    raise
            else:
                if row["state"] in {"DELETING", "DELETE_FAILED", "DELETED"}:
                    raise DomainError("该原始采集已删除或正在删除", 409)
                return row
            storage = {"robot_type": robot_type(manifest["profile"].get("robot_type"))}
            if runtime is not None:
                storage.update(
                    runtime_id=runtime.id,
                    provider=runtime.provider,
                    profile_hash=runtime.profile_hash,
                )
            if self.settings.data_root is not None:
                storage["data_layout"] = ROBOT_LAYOUT
            prepare_storage(self.settings, storage)
            row = {
                **storage,
                **collection_metadata(manifest, "imported", ""),
                "collection_layout": True,
                "id": key,
                "state": "COMMITTED",
                "source_kind": "raw_import",
                "imported": True,
                "manifest_hash": manifest_hash,
                "manifest_path": str(Path(manifest_path).resolve()),
                "manifest_inline": manifest if inline else None,
                "raw_path": str(Path(raw_path).resolve()),
                "import_path": str(Path(raw_path).resolve()),
                "session_id": manifest["session_id"],
                "robot_id": manifest["robot_id"],
                "project_id": manifest["project_id"],
                "target": "imported",
                "prefix": "",
                "created_at": manifest.get("started_at", now()),
                "committed_at": manifest.get("stopped_at", now()),
                "total_bytes": total,
                "file_count": len(manifest["files"]),
                "instruction": manifest["instruction"],
                "capture_mode": manifest["capture_mode"],
            }
            self.store.put("upload", key, row)
            return row

    def final_dir(self, row):
        suffix = row["id"]
        if row.get("collection_layout"):
            suffix = f"collections/{row['collection_id']}/episodes/{row['id']}"
        rel = "/".join(x for x in [row["target"], row["prefix"], suffix] if x)
        return storage_path(self.settings, row, "raw", rel)

    def root(self, row):
        final = self.final_dir(row)
        return (
            final
            if final.exists()
            else storage_path(self.settings, row, "incoming", row["id"])
        )

    def manifest(self, row, fresh=False):
        key = row["manifest_hash"]
        if not fresh:
            # Conversion workers read manifests concurrently.
            with self.lock:
                cached = self._manifest_cache.get(key)
            if cached is not None:
                return cached
        inline = row.get("manifest_inline")
        if isinstance(inline, dict):
            data = inline
        else:
            manifest_path = row.get("manifest_path")
            source = (
                Path(manifest_path)
                if manifest_path
                else self.root(row) / "manifest.json"
            )
            data = json.loads(source.read_text())
        if digest(data) != key:
            raise DomainError("Manifest 摘要不匹配", 422)
        with self.lock:
            if len(self._manifest_cache) >= 4:
                self._manifest_cache.pop(next(iter(self._manifest_cache)))
            self._manifest_cache[key] = data
        return data

    def offsets(self, uid):
        with self._upload_lock(uid):
            row = self.store.get("upload", uid)
            if row["state"] in {"DELETING", "DELETE_FAILED", "DELETED"}:
                raise DomainError("原始采集已删除或正在删除", 410)
            root = self.root(row) / "data"
            return {
                "id": uid,
                "state": row["state"],
                "manifest_hash": row["manifest_hash"],
                "chunk_bytes": self.settings.chunk_bytes,
                "offsets": [
                    confined(root, f["path"]).stat().st_size
                    if confined(root, f["path"]).exists()
                    else 0
                    for f in self.manifest(row)["files"]
                ],
            }

    def chunk(self, uid, file_index, offset, expected_hash, data):
        with self._upload_lock(uid):
            row = self.store.get("upload", uid)
            if row["state"] != "UPLOADING":
                raise DomainError("上传已提交或正在提交")
            files = self.manifest(row)["files"]
            if not 0 <= file_index < len(files) or offset < 0:
                raise DomainError("非法文件索引或 offset", 400)
            spec = files[file_index]
            if (
                len(data) > self.settings.chunk_bytes
                or offset + len(data) > spec["size_bytes"]
                or (not data and spec["size_bytes"] != 0)
            ):
                raise DomainError("分块大小超限", 413)
            if hashlib.sha256(data).hexdigest() != expected_hash:
                raise DomainError("分块校验失败", 422)
            p = confined(self.root(row) / "data", spec["path"])
            data_directory(
                self.settings,
                row,
                "incoming",
                str(Path(row["id"], "data", spec["path"]).parent),
            )
            size = p.stat().st_size if p.exists() else 0
            if offset < size:
                with p.open("rb") as f:
                    f.seek(offset)
                    if f.read(len(data)) != data:
                        raise DomainError("重复分块内容不一致")
                return {"offset": size}
            if offset != size:
                raise DomainError("分块位置不匹配，请查询服务端进度")
            if (
                shutil.disk_usage(p.parent).free
                < len(data) + self.settings.min_free_bytes
            ):
                raise DomainError("服务器磁盘低水位", 507)
            with p.open("ab") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            share_data_path(p)
            # A crash during an append may leave a partial chunk. Resume uses exact byte offset;
            # final SHA-256 verification protects against torn/corrupt persisted data.
            return {"offset": p.stat().st_size}

    def reset_file(self, uid, index):
        with self._upload_lock(uid):
            row = self.store.get("upload", uid)
            if row["state"] != "UPLOADING":
                raise DomainError("仅未提交上传可以重置损坏文件")
            files = self.manifest(row)["files"]
            if not 0 <= index < len(files):
                raise DomainError("文件不存在", 404)
            p = confined(self.root(row) / "data", files[index]["path"])
            if p.exists():
                p.unlink()
                fsync_dir(p.parent)
            return {"offset": 0}

    def complete(self, uid):
        with self._upload_lock(uid):
            row = self.store.get("upload", uid)
            if row["state"] in {"DELETING", "DELETE_FAILED", "DELETED"}:
                raise DomainError("原始采集已删除或正在删除", 410)
            if row["state"] == "COMMITTED":
                return row
            root = self.root(row)
            manifest = self.manifest(row, fresh=True)
            if digest(manifest) != row["manifest_hash"]:
                raise DomainError("Manifest 已被修改", 422)
            for i, f in enumerate(manifest["files"]):
                p = confined(root / "data", f["path"])
                if (
                    not p.exists()
                    or p.stat().st_size != f["size_bytes"]
                    or file_hash(p) != f["sha256"]
                ):
                    raise DomainError(f"文件不完整或摘要不符：file_index={i}", 422)
            if row.get("runtime_id"):
                runtime = self.catalog.get(row["runtime_id"])
                validate = getattr(
                    self.catalog.provider(runtime), "validate_upload", None
                )
                if validate is not None:
                    validate(root / "data", manifest, runtime.profile)
            for d in sorted(
                (p for p in root.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts),
                reverse=True,
            ):
                fsync_dir(d)
            fsync_dir(root)
            row["state"] = "COMMITTING"
            self.store.put("upload", uid, row)
            final = self.final_dir(row)
            data_directory(
                self.settings,
                row,
                "raw",
                str(final.parent.relative_to(storage_path(self.settings, row, "raw"))),
            )
            share_data_tree(root)
            if root != final:
                os.replace(root, final)
                fsync_dir(final.parent)
                fsync_dir(storage_path(self.settings, row, "incoming"))
            row.update(
                state="COMMITTED", committed_at=now(), raw_path=str(final / "data")
            )
            self.store.put("upload", uid, row)
            return row

    def reconcile(self):
        for row in self.store.all("upload"):
            if row["state"] == "COMMITTING":
                self.complete(row["id"])
