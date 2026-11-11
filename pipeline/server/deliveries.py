"""Incrementally discover immutable READY deliveries on trusted local mounts.

The scanner owns a separate loop and never holds admission locks while hashing,
decoding video or running Parquet validation. Its durable intent precedes job
registration, so a crash between the two writes can safely finish on restart.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

from pipeline.shared.core import DomainError, digest, now

from .delivery_validation import header, inventory, read_json, validate_dataset


class DeliveryScanner:
    def __init__(self, settings, imports, datasets, runner):
        self.settings, self.imports, self.datasets, self.runner = (
            settings,
            imports,
            datasets,
            runner,
        )
        self.store = imports.store
        self.lock = threading.Lock()
        self.requested = threading.Event()
        self.stopping = threading.Event()
        self.last_scan = 0.0

    def request(self):
        self.requested.set()
        return {"requested": True, "enabled": bool(self.settings.delivery_sources)}

    def status(self):
        rows = self.store.all("delivery")
        # File inventories and per-episode IDs belong in private state, not polling.
        fields = (
            "id",
            "delivery_id",
            "dataset_id",
            "batch_path",
            "robot_type",
            "state",
            "error",
            "job_id",
            "attempts",
            "created_at",
            "updated_at",
            "next_retry_at",
            "conflict",
        )
        return {
            "enabled": bool(self.settings.delivery_sources),
            "scan_seconds": self.settings.delivery_scan_seconds,
            "requested": self.requested.is_set(),
            "sources": self.store.all("delivery_source"),
            "counts": {
                state: sum(r["state"] == state for r in rows)
                for state in {r["state"] for r in rows}
            },
            "deliveries": [{k: r[k] for k in fields if k in r} for r in rows[:500]],
        }

    def _get(self, kind, key):
        try:
            return self.store.get(kind, key)
        except DomainError as exc:
            if exc.code != 404:
                raise
            return None

    def _source_status(self, source, error):
        key = digest(source)[:32]
        self.store.put(
            "delivery_source",
            key,
            {
                "id": key,
                **source,
                "checked_at": now(),
                "state": "UNAVAILABLE" if error else "READY",
                "error": error,
            },
        )

    def _discover(self, source):
        root = Path(source["root"])
        if root.resolve() != root or not stat.S_ISDIR(root.lstat().st_mode):
            raise DomainError("扫描根目录不可用或包含符号链接", 503)
        # Exactly robot/task/batch; never recursively walk large dataset payloads.
        for task in sorted(root.iterdir()):
            if task.name.startswith(".") or not stat.S_ISDIR(task.lstat().st_mode):
                continue
            for batch in sorted(task.iterdir()):
                if batch.name.startswith(".") or not stat.S_ISDIR(
                    batch.lstat().st_mode
                ):
                    continue
                if os.path.lexists(batch / "READY"):
                    yield batch

    def _error(self, batch, source, exc):
        key = digest({"invalid_batch": str(batch)})[:32]
        old = self._get("delivery", key) or {}
        self.store.put(
            "delivery",
            key,
            {
                "id": key,
                "batch_path": str(batch),
                "robot_type": source["robot_type"],
                "state": "FAILED",
                "error": str(exc)[:2000],
                "created_at": old.get("created_at", now()),
                "updated_at": now(),
            },
        )

    def _claims(self, row, data):
        records = [
            r
            for r in self.store.all("delivery")
            if r["id"] != row["id"] and r.get("claimed")
        ]
        ids = set(data["episode_ids"])
        for previous in records:
            if ids.intersection(previous.get("episode_ids", [])):
                raise DomainError("episode_id 已由其他批次交付，拒绝重复轨迹", 409)
            if (
                previous.get("schema_key") == row["schema_key"]
                and previous.get("feature_digest") != row["feature_digest"]
            ):
                raise DomainError(
                    "同一 feature_schema_id 的字段定义发生变化，请升级语义版本", 409
                )

    def _process(self, batch, source, force=False):
        data = header(batch, source, self.settings)
        invalid_key = digest({"invalid_batch": str(batch)})[:32]
        if self._get("delivery", invalid_key):
            self.store.delete("delivery", invalid_key)
        key = digest({"delivery_id": data["delivery_id"]})[:32]
        row = self._get("delivery", key)
        if row and (
            row["manifest_sha256"] != data["manifest_sha256"]
            or row["batch_path"] != str(batch)
        ):
            row.update(conflict="相同 delivery_id 出现不同清单或目录", updated_at=now())
            self.store.put("delivery", key, row)
            raise DomainError("delivery_id 冲突；不可覆盖或搬移已交付批次", 409)
        if row and row["state"] == "REGISTERED":
            # Cheap inventory change detection; do not rehash/redecode unchanged data.
            try:
                if inventory(
                    batch, data, self.settings, hash_files=False, stopping=self.stopping
                ) != [tuple(s) for s in row["inventory"]]:
                    raise DomainError("已登记不可变批次的文件发生变化", 409)
                if row.get("conflict"):
                    row.update(conflict=None, updated_at=now())
                    self.store.put("delivery", key, row)
            except (DomainError, OSError) as exc:
                row.update(conflict=str(exc), updated_at=now())
                self.store.put("delivery", key, row)
            return False
        if row and not force and row.get("next_retry_at", 0) > now():
            return False
        row = row or {
            "id": key,
            "delivery_id": data["delivery_id"],
            "manifest_sha256": data["manifest_sha256"],
            "batch_path": str(batch),
            "robot_type": data["robot_type"],
            "dataset_id": data["dataset_id"],
            "created_at": now(),
        }
        row.update(
            state="VALIDATING",
            error=None,
            updated_at=now(),
            attempts=row.get("attempts", 0) + 1,
        )
        self.store.put("delivery", key, row)
        try:
            before = inventory(batch, data, self.settings, stopping=self.stopping)
            result = validate_dataset(
                batch, data, self.settings, stopping=self.stopping
            )
            info = read_json(
                batch / "lerobot/meta/info.json", self.settings.max_manifest_bytes
            )
            row.update(
                episode_ids=data["episode_ids"],
                schema_key=digest(
                    {
                        "robot_type": data["robot_type"],
                        "schema": data["feature_schema_id"],
                    }
                ),
                feature_digest=digest(info["features"]),
            )
            self._claims(row, data)
            if self.stopping.is_set():
                raise DomainError("Server 正在停止，交付将在下次扫描重试", 503)
            if (
                inventory(
                    batch, data, self.settings, hash_files=False, stopping=self.stopping
                )
                != before
                or header(batch, source, self.settings)["manifest_sha256"]
                != data["manifest_sha256"]
            ):
                raise DomainError("校验期间交付内容发生变化", 409)
            # Claim IDs durably before registration; safe to retry either side of a crash.
            row.update(claimed=True, inventory=before, validation=result)
            self.store.put("delivery", key, row)
            with self.runner.lock, self.imports.ingest.lock:
                self.datasets.check_import(str(batch / "lerobot"))
                path = self.imports.resolve(str(batch / "lerobot"))
                registered = self.imports._import_lerobot(
                    path, data["robot_type"], delivery=data
                )
            row.update(
                state="REGISTERED",
                job_id=registered["job_id"],
                error=None,
                next_retry_at=0,
                updated_at=now(),
            )
        except Exception as exc:
            row.update(
                state="FAILED",
                error=str(exc)[:2000],
                next_retry_at=now() + self.settings.delivery_retry_seconds,
                updated_at=now(),
            )
        self.store.put("delivery", key, row)
        return True

    def tick(self):
        if not self.settings.delivery_sources or self.stopping.is_set():
            return False
        if not self.lock.acquire(blocking=False):
            return False
        try:
            force = self.requested.is_set()
            if (
                not force
                and now() - self.last_scan < self.settings.delivery_scan_seconds
            ):
                return False
            self.requested.clear()
            self.last_scan = now()
            attempted = 0
            for source in self.settings.delivery_sources:
                try:
                    # Claims are private scanner state; missing sources never mean deletion.
                    for record in self.store.all("delivery"):
                        path = Path(record["batch_path"])
                        if (
                            record.get("claimed")
                            and path.is_relative_to(Path(source["root"]))
                            and (
                                not path.is_dir() or not os.path.lexists(path / "READY")
                            )
                        ):
                            record.update(
                                conflict="已发布批次目录或 READY 暂时不可访问；登记不会自动删除",
                                updated_at=now(),
                            )
                            self.store.put("delivery", record["id"], record)
                    for batch in self._discover(source):
                        if self.stopping.is_set():
                            break
                        try:
                            attempted += int(self._process(batch, source, force=force))
                        except (
                            DomainError,
                            OSError,
                            ValueError,
                            TypeError,
                            KeyError,
                        ) as exc:
                            self._error(batch, source, exc)
                        if attempted >= self.settings.delivery_scan_batch_limit:
                            break
                    self._source_status(source, None)
                except (DomainError, OSError) as exc:
                    self._source_status(source, str(exc))
                if attempted >= self.settings.delivery_scan_batch_limit:
                    break
            return True
        finally:
            self.lock.release()
