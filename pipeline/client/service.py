from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
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

from .adapters.registry import create_adapter
from .packaging import raw_format, seal_capture
from .resources import BackgroundWork, WorkPaused

BUSY = {"STARTING", "RECORDING", "STOPPING", "RECOVERY_REQUIRED"}


class CaptureService:
    def __init__(self, settings, adapter=None):
        self.settings = settings
        self.store = Store(settings.root / "state", legacy_name="client.sqlite")
        self.profile = settings.profile()
        self.adapter = adapter or create_adapter(settings, self.profile)
        self.capabilities = getattr(self.adapter, "capabilities", {})
        self.lock = threading.RLock()
        self.seal_lock = threading.Lock()
        self.roots = {
            k: Path(v).expanduser().absolute() for k, v in settings.local_roots.items()
        } or {"default": settings.root / "captures"}
        for root in self.roots.values():
            if root.resolve() != root:
                raise ValueError("本地受管根不得含符号链接")
            root.mkdir(parents=True, exist_ok=True)
        self.background = BackgroundWork(self)
        self.batch_lock = threading.RLock()
        self.batch_active = threading.Event()
        for batch in self.store.all("batch"):
            if batch["state"] != "ENDED":
                self.batch_active.set()
                if self.capabilities.get("external_trigger"):
                    self.adapter.recover(batch)
                break

    def update(self, cid, **changes):
        """Merge worker progress into the latest record, never a stale snapshot."""
        with self.lock:
            row = self.store.get("capture", cid)
            row.update(changes)
            return self.save(row)

    def current_batch(self):
        return next((b for b in self.store.all("batch") if b["state"] != "ENDED"), None)

    def background_blocker(self):
        if self.batch_active.is_set():
            return "采集作业进行中或待恢复，后台处理暂停"
        if getattr(self, "replay", None) and self.replay.row["state"] in {
            "PREPARING",
            "RUNNING",
            "STOPPING",
            "RECOVERY_REQUIRED",
        }:
            return "回放进行中或待恢复"
        if self.capabilities.get("external_trigger"):
            # No filesystem-wide state scans in a per-chunk hot path. The batch
            # gate is cleared only after recorder exit and final reconciliation.
            return ""
        if any(
            r["state"] in BUSY or r["state"] in {"PAUSED", "SAVING"}
            for r in self.store.all("capture")
        ):
            return "录制或保存状态未结束"
        return ""

    def begin_batch(self, body):
        if not self.capabilities.get("external_trigger"):
            raise DomainError("当前 Adapter 不支持外部触发作业")
        with self.batch_lock:
            current = self.current_batch()
            if current:
                if current["idempotency_key"] == body["idempotency_key"]:
                    if current["request_hash"] != digest(
                        {k: v for k, v in body.items() if k != "idempotency_key"}
                    ):
                        raise DomainError("同一幂等键不能用于不同作业")
                    return current
                raise DomainError("已有采集作业，请先结束或恢复")
            if getattr(self, "replay", None) and self.replay.row["state"] in {
                "PREPARING",
                "RUNNING",
                "STOPPING",
                "RECOVERY_REQUIRED",
            }:
                raise DomainError("请先结束回放")
            for previous in self.store.all("batch"):
                if previous["idempotency_key"] == body["idempotency_key"]:
                    raise DomainError("已结束的作业不能重启，请使用新的请求编号")
            # DRAFT batch is persisted before revoking background permission.
            row = self.create(body)
            batch = {**row, "state": "PREPARING", "episode_count": 0}
            self.batch_active.set()
            self.store.put("batch", batch["id"], batch)
            # The template is not an episode and never enters the upload queue.
            self.store.delete("capture", row["id"])
            deadline = time.monotonic() + 15
            while not self.background.serial.acquire(timeout=0.1):
                if time.monotonic() > deadline:
                    batch.update(
                        state="RECOVERY_REQUIRED",
                        error="后台任务尚未让出，请结束作业后重试",
                    )
                    self.store.put("batch", batch["id"], batch)
                    return batch
            try:

                def persist(session):
                    batch["adapter_session"] = session
                    self.store.put("batch", batch["id"], batch)

                if (
                    shutil.disk_usage(self.roots[batch["local_root"]]).free
                    < self.settings.min_free_bytes
                ):
                    raise DomainError("磁盘剩余容量不足")
                batch["launch_requested"] = True
                self.store.put("batch", batch["id"], batch)
                self.adapter.begin(batch, persist)
                batch.update(state="ACTIVE", error=None)
            except Exception as exc:
                if getattr(self.adapter, "launch_attempted", None) is False:
                    batch["launch_requested"] = False
                batch.update(state="RECOVERY_REQUIRED", error=str(exc))
            finally:
                self.background.serial.release()
            self.store.put("batch", batch["id"], batch)
            return batch

    def sync_external(self, final=False):
        if not self.capabilities.get("external_trigger"):
            return
        with self.batch_lock:
            batch = self.current_batch()
            if not batch:
                return
            if not final and not batch.get("adapter_session"):
                return  # Never turn a failed preflight/unknown launch into ACTIVE.
            try:
                observations = self.adapter.observe()
                if final:
                    observed = {
                        digest({"batch": batch["id"], "episode": o["key"]})[:32]
                        for o in observations
                    }
                    if any(
                        r.get("batch_id") == batch["id"] and r["id"] not in observed
                        for r in self.store.all("capture")
                    ):
                        raise DomainError("最终核对发现已登记 episode 元数据缺失")
                for item in observations:
                    key = digest({"batch": batch["id"], "episode": item["key"]})[:32]
                    with self.lock:
                        try:
                            row = self.store.get("capture", key)
                        except DomainError as exc:
                            if exc.code != 404:
                                raise
                            row = {
                                **batch,
                                "id": key,
                                "batch_id": batch["id"],
                                "created_at": item["started_at"],
                                "raw_root": str(
                                    Path(batch["raw_root"]).parent
                                    / "episodes"
                                    / key
                                    / "raw"
                                ),
                                "upload_state": "NONE",
                                "state": "STARTING",
                                "external_trigger": True,
                                "source_episode_index": item["index"],
                                "started_at": item["started_at"],
                            }
                            row.pop("adapter_session", None)
                        # Once final metadata is registered, mutation is an error,
                        # never silently reset a sealed/uploaded episode.
                        metadata_hash = item["metadata_hash"]
                        if row.get("source_metadata_hash"):
                            if row["source_metadata_hash"] != metadata_hash:
                                raise DomainError("已登记 episode 元数据发生变化")
                            continue
                        state = item["state"]
                        if state not in {
                            "RECORDING",
                            "PAUSED",
                            "SAVING",
                            "SEALING",
                            "DISCARDED",
                            "REVIEW",
                        }:
                            raise DomainError("未知原始 episode 状态")
                        row.update(
                            state=state,
                            source_directory=item["source_directory"],
                            metadata_path=item["metadata_path"],
                            robot_config_path=item["robot_config_path"],
                            error=item.get("error"),
                            frame_count=item["count"],
                            count_unit=item["count_unit"],
                        )
                        if item["final"]:
                            row.update(
                                source_metadata_hash=metadata_hash,
                                stopped_at=item["stopped_at"],
                                capture_failed=state == "REVIEW",
                                total_bytes=item["size_bytes"],
                            )
                        self.save(row)
                status = {"state": "STOPPED"} if final else self.adapter.status()
                if not final:
                    for row in self.store.all("capture"):
                        if (
                            row.get("batch_id") == batch["id"]
                            and not row.get("source_metadata_hash")
                            and row.get("source_episode_index")
                            == status.get("episode_index")
                        ):
                            live_state = status.get("state")
                            if live_state not in {"RECORDING", "PAUSED", "SAVING"}:
                                live_state = None
                            if live_state:
                                self.update(
                                    row["id"],
                                    state=live_state,
                                    frame_count=status.get(
                                        "frame_count", row.get("frame_count", 0)
                                    ),
                                )
                    if (
                        Path(batch["raw_root"]).exists()
                        and shutil.disk_usage(batch["raw_root"]).free
                        < self.settings.min_free_bytes
                    ):
                        self.adapter.interrupt()
                        status = {
                            **status,
                            "error": "磁盘低水位，raw 采集器已请求中断；机器人控制不受此操作管理",
                        }
                batch.update(adapter_status=status, error=status.get("error"))
                if status.get("error"):
                    batch["state"] = "RECOVERY_REQUIRED"
                elif batch["state"] == "RECOVERY_REQUIRED":
                    batch["state"] = "ACTIVE"
                batch["episode_count"] = len(observations)
            except Exception as exc:
                batch.update(state="RECOVERY_REQUIRED", error=str(exc))
                self.store.put("batch", batch["id"], batch)
                if final:
                    raise
            self.store.put("batch", batch["id"], batch)

    def end_batch(self, bid):
        with self.batch_lock:
            batch = self.store.get("batch", bid)
            if batch["state"] == "ENDED":
                return batch
            try:
                if batch.get("adapter_session"):
                    self.adapter.end()
                    # Reattach the persisted paths only for final reconciliation.
                    self.adapter.recover(batch)
                    self.sync_external(final=True)
                    self.adapter.release()
                elif batch.get("launch_requested"):
                    raise DomainError("采集器归属不明，拒绝解除后台任务门控")
                batch = self.store.get("batch", bid)
                pending = [
                    r
                    for r in self.store.all("capture")
                    if r.get("batch_id") == bid
                    and r["state"] in {"RECORDING", "PAUSED", "STARTING", "SAVING"}
                ]
                for row in pending:
                    self.update(
                        row["id"],
                        state="REVIEW",
                        capture_failed=True,
                        error="采集器已退出，但 episode 未完成保存，请检查原始文件",
                    )
                batch.update(state="ENDED", ended_at=now(), error=None)
            except Exception as exc:
                batch.update(state="RECOVERY_REQUIRED", error=str(exc))
            self.store.put("batch", bid, batch)
            if batch["state"] == "ENDED":
                self.batch_active.clear()
            return batch

    def recover(self):
        for row in self.store.all("capture"):
            if row["state"] in BUSY and not row.get("external_trigger"):
                row.update(
                    state="RECOVERY_REQUIRED",
                    error="Client 重启，需确认 recorder 状态后结束；不能重发 start",
                )
                self.save(row)
            if row["state"] == "SEALING":
                row.update(error="封存被中断，后台将重新校验已保留的数据")
                self.save(row)
            if self._seal_failed(row) and not row.get("capture_failed"):
                # Older builds left a failed seal as a plain REVIEW row, which
                # kept offering "keep" for data that can never be packaged.
                row.update(capture_failed=True, seal_failed=True)
                self.save(row)

    @staticmethod
    def _seal_failed(row):
        """A REVIEW row whose seal attempt failed and produced no package."""

        if row.get("state") != "REVIEW" or not row.get("error"):
            return False
        if row.get("manifest_hash") or row.get("package_path"):
            return False
        return bool(
            row.get("seal_requested_at")
            or row.get("seal_started_at")
            or row.get("seal_duration_ms") is not None
        )

    def save(self, row):
        row["updated_at"] = now()
        self.store.put("capture", row["id"], row)
        return row

    def create(self, body):
        with self.lock:
            key = body["idempotency_key"]
            payload_hash = digest(
                {k: v for k, v in body.items() if k != "idempotency_key"}
            )
            for row in self.store.all("capture"):
                if row["idempotency_key"] == key:
                    if row["request_hash"] != payload_hash:
                        raise DomainError("同一幂等键不能用于不同请求")
                    return row
            root_id = body["local_root"]
            if root_id not in self.roots:
                raise DomainError("未知本地存储根", 400)
            prefix = safe_relative(body.get("local_prefix", ""), True)
            remote_prefix = safe_relative(body.get("remote_prefix", ""), True)
            root = self.roots[root_id]
            if prefix:
                root = confined(root, prefix)
            root.mkdir(parents=True, exist_ok=True)
            capture_id = str(uuid.uuid4())
            name = dataset_name(body.get("dataset_name") or body.get("instruction"))
            metadata = collection_metadata(
                {
                    "dataset_name": name,
                    "project_id": self.settings.project_id,
                    "profile": self.profile,
                    "profile_hash": digest(self.profile),
                    "capture_mode": self.capture_mode,
                    "raw_format": self.raw_format,
                },
                body["remote_target"],
                remote_prefix,
            )
            raw = (
                root
                / "datasets"
                / metadata["collection_id"]
                / "sessions"
                / capture_id
                / "raw"
            )
            if self.settings.mode == "ros2" and not re.fullmatch(
                r"/[A-Za-z0-9_./-]+", str(raw)
            ):
                raise DomainError(
                    "旧 recorder 的 shell 路径限制：本地根只能包含受控 ASCII 路径字符",
                    400,
                )
            row = {
                **metadata,
                "id": capture_id,
                "idempotency_key": key,
                "request_hash": payload_hash,
                "state": "DRAFT",
                "upload_state": "NONE",
                "created_at": now(),
                "instruction": body.get("instruction", ""),
                "local_root": root_id,
                "local_prefix": prefix,
                "raw_root": str(raw),
                "remote_target": body["remote_target"],
                "remote_prefix": remote_prefix,
                "robot_id": self.settings.robot_id,
                "project_id": self.settings.project_id,
                "profile": self.profile,
                "profile_hash": digest(self.profile),
                "mode": self.settings.mode,
                "raw_format": self.raw_format,
                "capture_mode": self.capture_mode,
                "error": None,
                "discard_requested": False,
            }
            return self.save(row)

    def describe(self, row):
        """Expose legacy captures in task groups without moving their files."""
        if row.get("collection_id"):
            return row
        metadata = collection_metadata(
            {
                "instruction": row["instruction"],
                "project_id": row["project_id"],
                "profile": row["profile"],
                "profile_hash": row["profile_hash"],
                "capture_mode": row.get(
                    "capture_mode", "real" if row["mode"] == "external" else row["mode"]
                ),
                "raw_format": row.get("raw_format", self.raw_format),
            },
            row["remote_target"],
            row.get("remote_prefix", ""),
        )
        return {**row, **metadata}

    def start(self, cid):
        if self.capabilities.get("external_trigger"):
            raise DomainError("请进入采集作业并使用手柄开始录制")
        with self.lock:
            if getattr(self, "replay", None) and self.replay.row["state"] in {
                "PREPARING",
                "RUNNING",
                "STOPPING",
                "RECOVERY_REQUIRED",
            }:
                raise DomainError("请先结束当前回放")
            row = self.store.get("capture", cid)
            if row["state"] in {"STARTING", "RECORDING"}:
                return row
            if row["state"] != "DRAFT":
                raise DomainError("该会话不能再次启动；请创建新会话")
            if any(r["state"] in BUSY for r in self.store.all("capture")):
                raise DomainError("机器人已有采集或状态未知的会话")
            health = self.adapter.health()
            if not health.get("ready"):
                raise DomainError(f"采集前检查失败：{health}")
            raw = Path(row["raw_root"])
            if raw.resolve() != raw or (raw / "episode0").exists():
                raise DomainError("拒绝符号链接或覆盖已有采集目录")
            raw.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(raw).free < self.settings.min_free_bytes:
                raise DomainError("磁盘剩余容量不足")
            row.update(state="STARTING", started_at=now(), error=None)
            self.save(row)  # Persist before the non-idempotent ROS call.
            try:
                self.adapter.start(
                    cid, raw, row.get("dataset_name") or row["instruction"]
                )
                row["state"] = "RECORDING"
            except Exception as exc:
                row.update(state="RECOVERY_REQUIRED", error=str(exc))
            return self.save(row)

    def stop(self, cid, discard=False, fault=None):
        with self.lock:
            row = self.store.get("capture", cid)
            if row.get("external_trigger"):
                raise DomainError(
                    "外部触发录制请使用手柄结束或丢弃；原始记录不由网页重写"
                )
            if row["state"] == "DISCARDED":
                return row
            if row["state"] in {"SEALED", "SEALING"}:
                raise DomainError("已封存数据不能用采集丢弃接口删除")
            if fault:
                row.update(capture_failed=True, error=fault)
                self.save(row)
            if discard:
                row["discard_requested"] = True
                self.save(row)
            if row["state"] in {"DRAFT", "REVIEW"}:
                if discard:
                    return self._discard(row)
                return row
            if row["state"] not in BUSY:
                raise DomainError("当前状态不允许停止")
            row["state"] = "STOPPING"
            self.save(row)
            try:
                result = self.adapter.stop(cid)
                if not result.get("stopped"):
                    raise DomainError("Recorder 未确认停止")
                row.update(
                    state="REVIEW",
                    stopped_at=now(),
                    capture_failed=bool(result.get("fail"))
                    or row.get("capture_failed", False),
                    error=fault or result.get("error") or row.get("error"),
                )
                self.save(row)
                return self._discard(row) if row["discard_requested"] else row
            except Exception as exc:
                row.update(state="RECOVERY_REQUIRED", error=str(exc))
                return self.save(row)

    def _discard(self, row):
        raw = Path(row["raw_root"])
        if raw.exists():
            if raw.resolve() != raw:
                raise DomainError("拒绝符号链接目录")
            trash = raw.parent / "discarded"
            os.replace(raw, trash)
            fsync_dir(trash.parent)
            row["trash_path"] = str(trash)
        row.update(state="DISCARDED", upload_state="NONE")
        return self.save(row)  # No automatic destructive deletion in v0.1.

    def request_keep(self, cid):
        with self.lock:
            row = self.store.get("capture", cid)
            if row["state"] in {"SEALED", "SEALING"}:
                return row
            if row["state"] != "REVIEW" or row["discard_requested"]:
                raise DomainError("只有已停止且未丢弃的会话可封存")
            if row.get("capture_failed"):
                raise DomainError("采集报告故障，不能作为正常数据封存；保留本地供排查")
            row.update(state="SEALING", seal_requested_at=now(), error=None)
            return self.save(row)

    def keep(self, cid):
        """Synchronous helper for offline callers; HTTP only queues request_keep."""
        self.request_keep(cid)
        return self.seal(cid)

    def seal_pending(self):
        rows = [r for r in self.store.all("capture") if r["state"] == "SEALING"]
        if rows:
            self.seal(rows[-1]["id"])
            return True
        return False

    @property
    def raw_format(self):
        return raw_format(self.capabilities)

    @property
    def capture_mode(self):
        # External hardware uses a transport-neutral category, not a ROS claim.
        return "real" if self.settings.mode == "external" else self.settings.mode

    def seal(self, cid):
        try:
            with self.background.lease():
                return self._seal(cid)
        except WorkPaused as exc:
            return self.update(cid, background_wait=str(exc))

    def _seal(self, cid):
        # Hashing and fsync may be slow. Serialize sealers, not robot control.
        with self.seal_lock:
            with self.lock:
                row = self.store.get("capture", cid)
                if row["state"] != "SEALING":
                    return row
                row.update(seal_started_at=now())
                self.save(row)
            started = time.perf_counter()
            try:
                root = Path(row["raw_root"])
                if root.resolve() != root or root.is_symlink():
                    raise DomainError("原始采集目录不能是符号链接")
                payload = seal_capture(
                    row, self.capabilities, self.background, hasher=file_hash
                )
                package_stats = payload["package_stats"]
                files = payload["files"]
                manifest = {
                    "manifest_version": 1,
                    "session_id": cid,
                    "robot_id": row["robot_id"],
                    "project_id": row["project_id"],
                    "raw_format": self.raw_format,
                    "capture_mode": self.capture_mode,
                    "control_mode": "teleoperation",
                    "profile": row["profile"],
                    "profile_hash": row["profile_hash"],
                    "instruction": row["instruction"],
                    "dataset_name": row.get("dataset_name") or row["instruction"],
                    "started_at": row["started_at"],
                    "stopped_at": row.get("stopped_at"),
                    "complete": True,
                    "files": files,
                }
                atomic_json(root.parent / "manifest.json", manifest)
                row.update(
                    state="SEALED",
                    manifest_hash=digest(manifest),
                    upload_state="QUEUED",
                    package_path=payload.get("package_path"),
                    package_schema=payload["package_schema"],
                    package_stats=package_stats,
                    file_count=len(files),
                    total_bytes=sum(f["size_bytes"] for f in files),
                    uploaded_bytes=0,
                    error=None,
                    sealed_at=now(),
                    background_wait=None,
                    **{
                        k: payload[k]
                        for k in ("upload_sources", "verified_files")
                        if k in payload
                    },
                )
            except WorkPaused as exc:
                row.update(background_wait=str(exc))
            except Exception as exc:
                # A seal failure means the raw data cannot be packaged; latch it
                # as a fault so the UI only offers discard and re-keep is refused,
                # instead of looping a "保留并上传" that keeps failing.
                row.update(
                    state="REVIEW",
                    capture_failed=True,
                    seal_failed=True,
                    error=str(exc),
                )
            row["seal_duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
            with self.lock:
                return self.save(row)

    def health(self):
        try:
            health = self.adapter.health()
            status = self.adapter.status()
            return {**health, "status": status}
        except Exception as exc:
            return {"ready": False, "mode": self.settings.mode, "detail": str(exc)}
