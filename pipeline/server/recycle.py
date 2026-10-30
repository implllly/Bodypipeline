"""Persistent, dataset-scoped Episode recycle bin for the viewer gateway."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from pipeline.shared.core import DomainError, Store, confined, executable_path

from .datasets import _dataset_roots


def record_cleanup(row):
    """Journal completed renumberings once, including pre-journal deployments."""
    op = row.get("operation") or {}
    if op.get("state") != "completed" or "mapping" not in op:
        return False
    history = row.setdefault("cleanup_history", [])
    if any(item["id"] == op["id"] for item in history):
        return False
    generation = row.get("cleanup_generation", 0)
    history.append(
        {
            "id": op["id"],
            "from": generation,
            "to": generation + 1,
            "mapping": op["mapping"],
        }
    )
    row["cleanup_generation"] = generation + 1
    # Maps can be large. Older browsers outside this window must recheck flags.
    row["cleanup_history"] = history[-32:]
    return True


@contextlib.contextmanager
def dataset_lock(store: Store, key: str):
    directory = store.path / "recycle-locks"
    directory.mkdir(exist_ok=True)
    with (directory / f"{key}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DomainError("该数据集正在处理，请稍后重试", 409) from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class RecycleBin:
    def __init__(self, settings, store, registry):
        self.settings, self.store, self.registry = settings, store, registry

    def resolve(self, repo: str) -> tuple[str, Path]:
        preview = self.registry.lookup(repo)
        if preview is None:
            raise DomainError("预览已过期，请从管理台重新打开数据集", 404)
        raw = Path(preview["dataset_path"])
        for root in _dataset_roots(self.settings):
            root = root.resolve()
            try:
                relative = raw.relative_to(root).as_posix()
                # A configured storage root itself must never become a deletion target.
                if relative == ".":
                    continue
                path = confined(root, relative)
            except (ValueError, DomainError):
                continue
            if path.is_dir() and (path / "meta/info.json").is_file():
                key = hashlib.sha256(str(path).encode()).hexdigest()
                return key, path
        raise DomainError("数据集路径不在允许的数据目录内", 403)

    def _load(self, key, path):
        try:
            return self.store.get("recycle", key)
        except DomainError as exc:
            if exc.code != 404:
                raise
        return {
            "id": key,
            "dataset_path": str(path),
            "entries": {},
            "revision": 0,
            "created_at": time.time(),
            "operation": None,
        }

    @staticmethod
    def _info(path):
        try:
            info = json.loads((path / "meta/info.json").read_text())
            if info.get("codebase_version") not in {"v2.1", "v3.0"}:
                raise ValueError("仅支持 LeRobot v2.1 / v3.0")
            int(info["total_episodes"])
            return info
        except (OSError, ValueError, KeyError) as exc:
            raise DomainError(f"无法读取数据集元数据：{exc}", 409) from exc

    def _recover(self, row):
        op = row.get("operation") or {}
        # An older failed/pending operation may have replaced the last completed
        # operation before history existed. Do not call that dataset generation 0.
        if "cleanup_generation" not in row and self._info(
            Path(row["dataset_path"])
        ).get("bodypipeline_revision"):
            row["cleanup_generation"] = 1
            row["cleanup_history"] = []
            self.store.put("recycle", row["id"], row)
        if op.get("state") not in {"running", "pending"}:
            if record_cleanup(row):
                self.store.put("recycle", row["id"], row)
            return row
        pid = op.get("pid")
        if pid:
            try:
                os.kill(pid, 0)
                command = Path(f"/proc/{pid}/cmdline").read_bytes()
                if (
                    b"pipeline.server.recycle_worker" in command
                    and op["id"].encode() in command
                ):
                    return row
            except ProcessLookupError:
                pass
            except FileNotFoundError:
                pass
        # The worker journal and the marker inside the published dataset resolve
        # a crash on either side of the atomic directory exchange.
        from .recycle_worker import recover_operation

        return recover_operation(self.store, row)

    def _public(self, row, path):
        info = self._info(path)
        operation = row.get("operation")
        if operation:
            operation = {
                key: value
                for key, value in operation.items()
                if key not in {"pid", "entries_before"}
            }
        return {
            "entries": sorted(row["entries"].values(), key=lambda e: e["episode"]),
            "revision": row["revision"],
            "operation": operation,
            "cleanup_generation": row.get("cleanup_generation", 0),
            "cleanup_history": row.get("cleanup_history", []),
            "total_episodes": int(info["total_episodes"]),
            "total_frames": int(info["total_frames"]),
            "dataset_name": path.name,
        }

    def status(self, repo):
        key, path = self.resolve(repo)
        try:
            with dataset_lock(self.store, key):
                row = self._recover(self._load(key, path))
        except DomainError as exc:
            if exc.code != 409:
                raise
            row = self._load(key, path)
        return self._public(row, path)

    def change(self, repo, action, episodes, revision):
        key, path = self.resolve(repo)
        with dataset_lock(self.store, key):
            # A whole-dataset deletion may revoke the preview after resolve but
            # before we acquire the lock. Never operate using that stale handle.
            if self.registry.lookup(repo) is None:
                raise DomainError("预览已关闭或数据集正在删除", 409)
            row = self._recover(self._load(key, path))
            if (row.get("operation") or {}).get("state") in {"running", "pending"}:
                raise DomainError("正在永久清理，请等待完成", 409)
            if row["revision"] != revision:
                raise DomainError("列表已更新，请刷新后重新选择", 409)
            info = self._info(path)
            if any(ep < 0 or ep >= int(info["total_episodes"]) for ep in episodes):
                raise DomainError("Episode 编号不存在", 404)
            if action != "trash" and any(
                str(ep) not in row["entries"] for ep in episodes
            ):
                raise DomainError("只能操作已在回收站中的 Episode", 409)
            if action == "purge":
                tool = executable_path(self.settings.letools_bin)
                if not tool:
                    raise DomainError("未安装数据清理所需的 letools 运行环境", 503)
                python = Path(tool).parent / "python"
                if not python.is_file():
                    raise DomainError("找不到 letools Python 运行环境", 503)
                op_id = uuid.uuid4().hex
                row["operation"] = {
                    "id": op_id,
                    "state": "pending",
                    "episodes": episodes,
                    "entries_before": row["entries"].copy(),
                    "started_at": time.time(),
                    "progress": "准备清理",
                    "pid": None,
                }
                row["revision"] += 1
                self.store.put("recycle", key, row)
                log = self.settings.root / "reports" / f"recycle-{op_id}.log"
                log.parent.mkdir(exist_ok=True, parents=True)
                try:
                    with log.open("ab") as output:
                        process = subprocess.Popen(
                            [
                                str(python),
                                "-m",
                                "pipeline.server.recycle_worker",
                                "--store",
                                str(self.store.path),
                                "--key",
                                key,
                                "--operation",
                                op_id,
                            ],
                            env=self.settings.tool_environment(),
                            stdout=output,
                            stderr=output,
                            start_new_session=True,
                        )
                    row["operation"]["pid"] = process.pid
                    self.store.put("recycle", key, row)
                    threading.Thread(target=process.wait, daemon=True).start()
                except OSError as exc:
                    row["operation"].update(state="failed", error=str(exc))
                    self.store.put("recycle", key, row)
                    raise DomainError("无法启动清理任务", 503) from exc
            else:
                for ep in episodes:
                    if action == "trash":
                        row["entries"].setdefault(
                            str(ep), {"episode": ep, "deleted_at": time.time()}
                        )
                    else:
                        row["entries"].pop(str(ep))
                row["revision"] += 1
                self.store.put("recycle", key, row)
        return self._public(row, path)
