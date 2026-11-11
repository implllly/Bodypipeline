from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any


class DomainError(Exception):
    def __init__(self, message: str, code: int = 409):
        self.message, self.code = message, code
        super().__init__(message)


def now() -> float:
    return time.time()


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def safe_relative(value: str, allow_empty: bool = False) -> str:
    if value == "" and allow_empty:
        return value
    p = PurePosixPath(value)
    if (
        not value
        or p.is_absolute()
        or "\\" in value
        or any(x in ("", ".", "..") for x in value.split("/"))
    ):
        raise DomainError("路径必须为不含 .、.. 或反斜杠的相对路径", 400)
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in p.parts):
        raise DomainError("路径仅允许英文字母、数字、下划线、连字符和点", 400)
    return value


def confined(root: Path, relative: str) -> Path:
    safe_relative(relative)
    root = root.resolve()
    p = root
    for component in PurePosixPath(relative).parts:
        p = p / component
        if p.is_symlink():
            raise DomainError("拒绝符号链接路径", 400)
    if not p.resolve().is_relative_to(root):
        raise DomainError("路径越界", 400)
    return p


def fsync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_mkdir(path: Path):
    """Persist each newly created directory entry, including intermediate parents."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        fsync_dir(directory.parent)


def atomic_json(path: Path, value: Any):
    data = canonical(value)
    durable_mkdir(path.parent)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


class Store:
    """Plain JSON files at <path>/<kind>/<key>.json; one service writer per root.

    Atomic replacement lets the separate, read-only viewer gateway observe complete
    records. Business locks and intent/recovery states protect multi-step operations.
    """

    def __init__(self, path: Path, *, legacy_name: str | None = None):
        self.path = path.absolute()
        self.lock = threading.RLock()
        if self.path.resolve() != self.path:
            raise DomainError("状态目录不得含符号链接", 500)
        # Never start an empty recorder history over existing, unmigrated state.
        if legacy_name and (self.path.parent / legacy_name).exists():
            marker = self.path / ".legacy-import.json"
            if not marker.exists() or self._read(marker).get("source") != legacy_name:
                raise RuntimeError(
                    "发现旧版状态文件；请先停止服务并运行 "
                    f"scripts/migrate_state.py --role {legacy_name.split('.')[0]} "
                    f"--root {self.path.parent}"
                )
        durable_mkdir(self.path)

    @staticmethod
    def _component(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", value
        ):
            raise DomainError("非法状态文件类型或 ID", 400)
        return value

    def _file(self, kind: str, key: str) -> Path:
        return confined(
            self.path, f"{self._component(kind)}/{self._component(key)}.json"
        )

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("不是普通文件")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("状态必须是 JSON 对象")
            # Reject non-finite numbers, including manually edited files.
            canonical(value)
            if "id" in value and value["id"] != path.stem:
                raise ValueError("记录 ID 与文件名不一致")
            return value
        except (OSError, ValueError) as exc:
            # Fail closed: skipping a corrupt capture could allow a second recorder.
            raise DomainError(
                f"状态文件损坏或不可读：{path}；请恢复文件后重试", 500
            ) from exc

    def put(self, kind: str, key: str, value: dict):
        path = self._file(kind, key)
        if not isinstance(value, dict) or ("id" in value and value["id"] != key):
            raise DomainError("状态必须是 JSON 对象且 ID 与文件名一致", 400)
        # Write to a unique temp file and replace atomically without holding the
        # process-wide lock. A stalled shared filesystem must not block reads of
        # unrelated state records.
        atomic_json(path, value)

    def get(self, kind: str, key: str) -> dict:
        with self.lock:
            path = self._file(kind, key)
            if not path.exists():
                raise DomainError("记录不存在", 404)
            return self._read(path)

    def all(self, kind: str) -> list[dict]:
        with self.lock:
            directory = confined(self.path, self._component(kind))
            try:
                # glob() may silently suppress directory access errors. A failed scan
                # must never look like an empty recorder or upload history.
                paths = sorted(directory.iterdir())
            except FileNotFoundError:
                return []
            except OSError as exc:
                raise DomainError(f"无法读取状态目录：{directory}", 500) from exc
            rows = []
            for path in paths:
                if path.suffix != ".json":
                    continue
                value = self._read(self._file(kind, path.stem))
                created = value.get("created_at", 0)
                if type(created) not in (int, float):
                    raise DomainError(f"状态文件 created_at 无效：{path}", 500)
                rows.append((created, path.name, value))
            # Updating progress must not move an old task behind newer queued jobs.
            return [row[2] for row in sorted(rows, key=lambda r: r[:2], reverse=True)]

    def delete(self, kind: str, key: str):
        with self.lock:
            path = self._file(kind, key)
            if path.exists():
                path.unlink()
                fsync_dir(path.parent)


class ProcessLock:
    def __init__(self, path: Path):
        self.path, self.file = path, None

    def acquire(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError(f"已有进程占用 {self.path}；只能使用一个 uvicorn worker")

    def release(self):
        if self.file:
            self.file.close()


def executable_path(name: str) -> str | None:
    import shutil
    import sys

    found = shutil.which(name)
    if not found and "/" not in name:
        sibling = Path(sys.executable).parent / name
        if sibling.is_file() and os.access(sibling, os.X_OK):
            found = str(sibling)
    return str(Path(found).resolve()) if found else None
