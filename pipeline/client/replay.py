"""Manage the installed replay tool; never implement robot motion here."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path

from pipeline.shared.core import DomainError, atomic_json

from .replay_providers.registry import create_provider

ACTIVE = {"PREPARING", "RUNNING", "STOPPING", "RECOVERY_REQUIRED"}


def process_identity(pid):
    """Linux start time prevents signalling a recycled PID during recovery."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


class ReplayService:
    def __init__(self, settings, capture):
        self.capture = capture
        self.lock = capture.lock
        self.config = settings.replay
        self.enabled = bool(self.config.get("enabled", False))
        self.provider = create_provider(self.config, capture.profile, settings.mode)
        self.capabilities = self.provider.capabilities if self.provider else None
        self.provider_name = self.capabilities.id if self.capabilities else None
        self.robot_type = capture.profile.get("robot_type", "")
        self.cache = settings.root / "replay-cache"
        if self.capabilities:
            self.cache = self.cache / self.capabilities.id / self.capabilities.version
        self.roots = {
            k: Path(v).absolute() for k, v in self.config.get("roots", {}).items()
        }
        self.roots.update(
            {f"capture:{key}": value for key, value in capture.roots.items()}
        )
        self.state_path = settings.root / "replay-state.json"
        self.row = {"state": "IDLE", "frame": 0, "frames": 0, "log": []}
        self.process = None
        self.worker = None
        self.stop_event = threading.Event()
        if self.state_path.exists():
            import json

            self.row = json.loads(self.state_path.read_text())
            if self.row["state"] in ACTIVE:
                self.row.update(
                    state="RECOVERY_REQUIRED", error="上次回放被中断；请先停止遗留进程"
                )

    def _save(self):
        atomic_json(self.state_path, self.row)

    def status(self):
        with self.lock:
            return {
                **self.row,
                "log": list(self.row["log"]),
                "enabled": self.enabled,
                "simulated": bool(self.capabilities and self.capabilities.simulated),
                "provider": self.capabilities.public() if self.capabilities else None,
                "robot_type": self.robot_type,
                "unavailable_reason": ""
                if self.provider
                else "当前 Client 尚未配置回放 Provider",
                "roots": [{"id": k, "path": str(v)} for k, v in self.roots.items()],
            }

    def _path(self, root, relative):
        if root not in self.roots:
            raise DomainError("未知回放目录", 400)
        base = self.roots[root]
        path = base / relative
        if (
            Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or base.resolve() != base
            or path.resolve() != path
            or not path.is_relative_to(base)
        ):
            raise DomainError("路径不在允许的回放目录内", 400)
        return path

    def files(self, root, directory=""):
        path = self._path(root, directory)
        if not path.is_dir():
            raise DomainError("目录不存在", 404)
        entries = []
        for item in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
            if item.is_symlink() or item.name.startswith("."):
                continue
            if item.is_dir() or (
                self.capabilities
                and item.suffix.lower() in self.capabilities.file_extensions
            ):
                entries.append(
                    {
                        "name": item.name,
                        "path": str(item.relative_to(self.roots[root])),
                        "directory": item.is_dir(),
                    }
                )
        return {"entries": entries[:500], "truncated": len(entries) > 500}

    def inspect(self, root, path):
        file = self._path(root, path)
        if not self.provider:
            return {
                "path": str(file),
                "supported": False,
                "frames": 0,
                "error": "当前 Client 尚未配置回放 Provider",
            }
        return {"path": str(file), **self.provider.probe(file)}

    def captures(self):
        result = []
        for row in self.capture.store.all("capture"):
            if row["state"] != "SEALED" or not row.get("package_path"):
                continue
            file = Path(row["package_path"])
            root = f"capture:{row['local_root']}"
            if root not in self.roots or not file.is_relative_to(self.roots[root]):
                continue
            result.append(
                {
                    "id": row["id"],
                    "collection_id": row.get("collection_id", row["id"]),
                    "dataset_name": row.get("dataset_name")
                    or row.get("instruction", ""),
                    "created_at": row["created_at"],
                    "root": root,
                    "path": str(file.relative_to(self.roots[root])),
                    "available": file.is_file(),
                }
            )
        return result

    def start(self, root, path, request_id):
        # Share the lifecycle lock, but never hold capture.lock while draining
        # background workers (their progress updates need that lock).
        with self.capture.batch_lock:
            if self.capture.current_batch():
                raise DomainError("请先结束当前采集作业")
            return self._start(root, path, request_id)

    def _start(self, root, path, request_id):
        from .service import BUSY

        with self.lock:
            if not self.enabled or not self.provider:
                raise DomainError("尚未配置回放工具", 503)
            if self.row.get("request_id") == request_id:
                if (root, path) != (self.row["root"], self.row["path"]):
                    raise DomainError("同一请求不能更换回放文件")
                return self.status()
            if self.row["state"] in ACTIVE:
                raise DomainError("已有回放正在运行")
            if any(r["state"] in BUSY for r in self.capture.store.all("capture")):
                raise DomainError("请先结束当前采集")
            file = self._path(root, path)
            info = self.provider.probe(file)
            if not info["supported"]:
                raise DomainError(info["error"], 400)
            self.row = {
                "state": "PREPARING",
                "request_id": request_id,
                "root": root,
                "path": path,
                "frames": info["frames"],
                "frame": 0,
                "started_at": time.time(),
                "error": "",
                "log": [],
                "pid": None,
                "process_identity": None,
                "format": info["format"],
                "fps": info.get("fps"),
                "provider_id": self.capabilities.id,
                "provider_version": self.capabilities.version,
                "robot_type": self.robot_type,
                "source_metadata": info.get("metadata", {}),
            }
            self._save()
            self.stop_event.clear()
            self.worker = threading.Thread(
                target=self._prepare_and_run, args=(file,), daemon=True
            )
            self.worker.start()
            return self.status()

    def _prepare_and_run(self, file):
        try:
            prepared = self.provider.prepare(file, self.cache)
            with self.lock:
                if self.stop_event.is_set():
                    self.row.update(state="STOPPED", ended_at=time.time())
                    self._save()
                    return
                command = self.provider.command(prepared)
                self.row.update(prepared_path=str(prepared), state="RUNNING")
                self._save()  # Record intent before launching; never replay on restart.
                self.process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    errors="replace",
                    bufsize=1,
                    start_new_session=True,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
                self.row.update(
                    pid=self.process.pid,
                    process_identity=process_identity(self.process.pid),
                )
                self._save()
            self._run(self.process)
        except Exception as exc:
            with self.lock:
                self.row.update(state="FAILED", error=str(exc), ended_at=time.time())
                self._save()

    @staticmethod
    def _terminate(process):
        # Only signal our process group. Never pkill other tools or drivers.
        for sig, timeout in (
            (signal.SIGINT, 4),
            (signal.SIGTERM, 2),
            (signal.SIGKILL, 1),
        ):
            if process.poll() is not None:
                return
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            try:
                process.wait(timeout=timeout)
                return
            except subprocess.TimeoutExpired:
                pass

    def _run(self, process):
        lines = queue.Queue(maxsize=2048)
        reader_done = threading.Event()
        reader_stop = threading.Event()

        def read():
            try:
                for line in process.stdout:
                    while not reader_stop.is_set():
                        try:
                            lines.put(line.rstrip(), timeout=0.1)
                            break
                        except queue.Full:
                            continue
                    if reader_stop.is_set():
                        break
            finally:
                process.stdout.close()
                reader_done.set()

        threading.Thread(target=read, daemon=True).start()
        result, error = "FAILED", "回放进程在最后一帧前退出"
        deadline = time.monotonic() + self.capabilities.progress_timeout
        try:
            while True:
                if self.stop_event.is_set():
                    result, error = "STOPPED", ""
                    break
                if time.monotonic() > deadline:
                    error = (
                        f"{self.capabilities.progress_timeout:g} 秒内没有收到回放进度"
                    )
                    break
                try:
                    line = lines.get(timeout=0.1)
                except queue.Empty:
                    if process.poll() is not None and reader_done.is_set():
                        break
                    continue
                with self.lock:
                    self.row["log"] = (self.row["log"] + [line])[-100:]
                    frame = self.provider.frame(line)
                    if frame is not None:
                        # A missing/out-of-order frame must not masquerade as completion.
                        if frame != self.row["frame"]:
                            error = f"回放帧序号异常：{frame}"
                            break
                        self.row["frame"] = frame + 1
                        deadline = time.monotonic() + self.capabilities.progress_timeout
                        if frame + 1 == self.row["frames"]:
                            result, error = "COMPLETED", ""
                            break
                    elif provider_error := self.provider.error(line):
                        error = provider_error
                        break
            # Providers normalize progress to sequential submitted-frame indices.
            # Once all frames are submitted, release this job's process group.
        except Exception as exc:
            error = str(exc)
        finally:
            reader_stop.set()
            self._terminate(process)
            with self.lock:
                self.row.update(
                    state=result,
                    error=error,
                    ended_at=time.time(),
                    returncode=process.poll(),
                )
                self._save()

    def stop(self):
        with self.lock:
            if self.row["state"] == "RECOVERY_REQUIRED":
                pid, identity = self.row.get("pid"), self.row.get("process_identity")
                if pid and identity and process_identity(pid) == identity:
                    # Require a local operator to stop the surviving original tool.
                    raise DomainError(
                        f"原回放进程 PID {pid} 仍在运行，请在机器人上停止它后重试"
                    )
                self.row.update(state="STOPPED", error="", ended_at=time.time())
                self._save()
            elif self.row["state"] in ACTIVE:
                self.row["state"] = "STOPPING"
                self.stop_event.set()
            return self.status()

    def close(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=9)
