from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

import httpx

from pipeline.shared.core import DomainError, digest

from .resources import WorkPaused, file_identity


class UploadAgent:
    def __init__(self, service):
        self.service = service
        self.settings = service.settings

    def process_once(self):
        for row in self.service.store.all("capture"):
            if (
                row["state"] != "SEALED"
                or row["upload_state"] not in {"QUEUED", "UPLOADING", "RETRY", "PAUSED"}
                or row.get("retry_at", 0) > time.time()
            ):
                continue
            self.upload(row)
            return True
        return False

    def upload(self, row):
        try:
            with self.service.background.lease():
                row = self.service.store.get("capture", row["id"])
                if row["state"] != "SEALED" or row["upload_state"] == "ACKED":
                    return
                self._upload(row)
        except WorkPaused as exc:
            self.service.update(
                row["id"], upload_state="PAUSED", background_wait=str(exc)
            )

    def _upload(self, row):
        work = self.service.background
        root = Path(row["raw_root"])
        package = Path(row["package_path"]) if row.get("package_path") else None
        upload_root = package.parent if package is not None else root
        manifest_path = root.parent / "manifest.json"
        row = self.service.update(
            row["id"], upload_state="UPLOADING", upload_error=None, background_wait=None
        )
        try:
            manifest = json.loads(manifest_path.read_text())
            if digest(manifest) != row["manifest_hash"]:
                raise DomainError("本地 Manifest 被修改，禁止上传")
            with httpx.Client(
                base_url=self.settings.server_url,
                headers={"Authorization": f"Bearer {self.settings.device_token}"},
                timeout=10,
                trust_env=False,
            ) as http:

                def call(method, path, **kw):
                    work.checkpoint()
                    r = http.request(method, path, **kw)
                    if r.status_code >= 400:
                        raise DomainError(f"服务器 {r.status_code}: {r.text[:400]}")
                    return r.json()

                info = call(
                    "POST",
                    "/device/uploads",
                    json={
                        "manifest": manifest,
                        "target": row["remote_target"],
                        "prefix": row["remote_prefix"],
                    },
                )
                uid = info["id"]
                self.service.update(row["id"], upload_id=uid)
                status = call("GET", f"/device/uploads/{uid}")
                if status["manifest_hash"] != row["manifest_hash"]:
                    raise DomainError("远端清单摘要不匹配")
                if (
                    len(status["offsets"]) != len(manifest["files"])
                    or type(status["chunk_bytes"]) is not int
                    or status["chunk_bytes"] <= 0
                    or any(
                        type(offset) is not int or not 0 <= offset <= spec["size_bytes"]
                        for offset, spec in zip(
                            status["offsets"], manifest["files"], strict=True
                        )
                    )
                ):
                    raise DomainError("远端上传进度无效")
                done = sum(status["offsets"])
                verified = row.get("verified_files", {})
                for i, spec in enumerate(manifest["files"]):
                    path = Path(
                        row.get("upload_sources", {}).get(
                            spec["path"], upload_root / spec["path"]
                        )
                    )
                    identity = file_identity(path)
                    if verified.get(spec["path"]) != identity:
                        if (
                            work.hash_file(path) != spec["sha256"]
                            or file_identity(path) != identity
                        ):
                            raise DomainError(f"封存后文件发生变化：{spec['path']}")
                        verified[spec["path"]] = identity
                        self.service.update(row["id"], verified_files=verified)
                    offset = status["offsets"][i]
                    with path.open("rb") as f:
                        f.seek(offset)
                        empty_pending = (
                            spec["size_bytes"] == 0 and status["state"] != "COMMITTED"
                        )
                        while offset < spec["size_bytes"] or empty_pending:
                            work.checkpoint()
                            if file_identity(path) != identity:
                                raise DomainError("上传源在传输过程中发生变化")
                            empty_pending = False
                            data = f.read(
                                min(
                                    self.settings.chunk_bytes,
                                    status["chunk_bytes"],
                                    work.config["chunk_bytes"],
                                )
                            )
                            if not data and offset < spec["size_bytes"]:
                                raise DomainError("上传源提前结束")
                            sent_at = time.monotonic()
                            result = call(
                                "PUT",
                                f"/device/uploads/{uid}/files/{i}",
                                params={"offset": offset},
                                content=data,
                                headers={
                                    "X-Chunk-SHA256": hashlib.sha256(data).hexdigest()
                                },
                            )
                            if result["offset"] != offset + len(data):
                                raise DomainError("远端分块进度不匹配")
                            done += result["offset"] - offset
                            offset = result["offset"]
                            row["uploaded_bytes"] = done
                            self.service.update(row["id"], uploaded_bytes=done)
                            if work.enabled:
                                work.wait(
                                    max(
                                        0,
                                        len(data) / work.config["rate_bytes_per_second"]
                                        - (time.monotonic() - sent_at),
                                    )
                                )
                for spec in manifest["files"]:
                    path = Path(
                        row.get("upload_sources", {}).get(
                            spec["path"], upload_root / spec["path"]
                        )
                    )
                    if file_identity(path) != verified[spec["path"]]:
                        raise DomainError("提交前发现上传源发生变化")
                try:
                    receipt = call("POST", f"/device/uploads/{uid}/complete")
                except DomainError as exc:
                    import re

                    match = re.search(r"file_index=(\d+)", str(exc))
                    if match:
                        # A torn chunk may have survived a host crash. Restart only that
                        # immutable source file; next iteration resumes all other files.
                        call(
                            "POST",
                            f"/device/uploads/{uid}/files/{int(match.group(1))}/reset",
                        )
                    raise
                if (
                    receipt["state"] != "COMMITTED"
                    or receipt["manifest_hash"] != row["manifest_hash"]
                ):
                    raise DomainError("没有有效持久化回执")
                prune_error = None
                if package is not None and root.exists():
                    try:
                        if root.resolve() != root or root.is_symlink():
                            raise OSError("raw directory is a symlink")
                        shutil.rmtree(root)
                    except OSError as exc:
                        prune_error = str(exc)
                row.update(
                    upload_state="ACKED",
                    receipt=receipt,
                    uploaded_bytes=row["total_bytes"],
                    attempts=0,
                    retry_at=0,
                    raw_pruned_at=time.time()
                    if package is not None and not prune_error
                    else None,
                    raw_prune_error=prune_error,
                )
        except WorkPaused as exc:
            row.update(upload_state="PAUSED", background_wait=str(exc))
        except Exception as exc:
            attempts = row.get("attempts", 0) + 1
            row.update(
                upload_state="RETRY",
                upload_error=str(exc),
                attempts=attempts,
                retry_at=time.time() + min(300, 2 ** min(attempts, 8)),
            )
        fields = (
            "upload_state",
            "upload_error",
            "background_wait",
            "uploaded_bytes",
            "receipt",
            "attempts",
            "retry_at",
            "raw_pruned_at",
            "raw_prune_error",
        )
        self.service.update(row["id"], **{k: row[k] for k in fields if k in row})
