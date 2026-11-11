"""Validate a complete immutable delivery before registration (no data writes)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import time
from datetime import datetime
from pathlib import Path

from pipeline.shared.core import (
    DomainError,
    canonical,
    confined,
    executable_path,
    file_hash,
)

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")


def read_json(path, limit):
    if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > limit:
        raise DomainError(f"JSON 不是普通文件或超过大小限制：{path.name}", 422)
    try:

        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("JSON 含重复键")
                value[key] = item
            return value

        value = json.loads(path.read_bytes(), object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError("需要 JSON 对象")
        canonical(value)
        return value
    except (ValueError, UnicodeError) as exc:
        raise DomainError(f"无效 JSON：{path.name}：{exc}", 422) from exc


def identity(value, name):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise DomainError(f"{name} 必须为 1–128 位字母数字、点、下划线或连字符", 422)
    return value


def header(batch, source, settings):
    """Cheap READY/manifest checks; called even for previously seen batches."""
    ready = read_json(batch / "READY", 4096)
    delivery_id = identity(ready.get("delivery_id"), "delivery_id")
    sha = ready.get("manifest_sha256")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise DomainError("READY 的 manifest_sha256 无效", 422)
    manifest = batch / "delivery.json"
    data = read_json(manifest, settings.max_manifest_bytes)
    if file_hash(manifest) != sha:
        raise DomainError("READY 与 delivery.json 的 SHA-256 不一致", 422)
    if data.get("schema") != "bodypipeline-lerobot-delivery/1":
        raise DomainError("不支持的交付 schema", 422)
    if data.get("delivery_id") != delivery_id:
        raise DomainError("READY 和 delivery.json 的 delivery_id 不一致", 422)
    for key in ("dataset_id", "feature_schema_id"):
        identity(data.get(key), key)
    if data.get("robot_type") != source["robot_type"]:
        raise DomainError("交付 robot_type 与可信扫描源绑定不一致", 422)
    if data.get("format") != "lerobot" or data.get("codebase_version") != "v3.0":
        raise DomainError("仅接受完整 LeRobot v3.0 交付", 422)
    if data.get("dataset_path") != "lerobot":
        raise DomainError("dataset_path 必须为 lerobot", 422)
    for key in ("total_frames", "total_episodes"):
        if type(data.get(key)) is not int or data[key] < 1:
            raise DomainError(f"{key} 必须为正整数", 422)
    fps = data.get("fps")
    if type(fps) not in (int, float) or not math.isfinite(fps) or fps <= 0:
        raise DomainError("fps 必须为有限正数", 422)
    ids = data.get("episode_ids")
    if not isinstance(ids, list) or len(ids) != data["total_episodes"]:
        raise DomainError("episode_ids 数量必须与 total_episodes 一致", 422)
    for value in ids:
        identity(value, "episode_id")
    if len(set(ids)) != len(ids):
        raise DomainError("批次包含重复 episode_id", 422)
    if data.get("supersedes_delivery_id") is not None:
        # No automatic retirement/replacement: this needs explicit operator approval.
        raise DomainError("替代批次需人工审核，暂不自动登记", 409)
    try:
        created = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
        if created.utcoffset() is None or created.utcoffset().total_seconds() != 0:
            raise ValueError("需要 UTC")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise DomainError("created_at 必须为 ISO 8601 UTC 时间", 422) from exc
    return {**data, "manifest_sha256": sha}


def inventory(batch, data, settings, *, hash_files=True, stopping=None):
    files = data.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= settings.max_files:
        raise DomainError("files 清单为空或超过数量限制", 422)
    declared, signatures = set(), []
    for item in files:
        if stopping is not None and stopping.is_set():
            raise DomainError("Server 停止，取消交付文件校验，下次扫描可重试", 503)
        if not isinstance(item, dict):
            raise DomainError("files 项必须是对象", 422)
        name, size, sha = item.get("path"), item.get("size_bytes"), item.get("sha256")
        if not isinstance(name, str) or not name.startswith("lerobot/"):
            raise DomainError("清单路径必须在 lerobot/ 内", 422)
        path = confined(batch, name)
        if name in declared:
            raise DomainError("files 清单含重复路径", 422)
        declared.add(name)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or type(size) is not int or size < 0:
            raise DomainError(f"文件类型或 size_bytes 无效：{name}", 422)
        if before.st_size != size or not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise DomainError(f"文件大小或 SHA-256 格式不符：{name}", 422)
        if hash_files:
            hashed = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    if stopping is not None and stopping.is_set():
                        raise DomainError(
                            "Server 停止，取消交付文件校验，下次扫描可重试", 503
                        )
                    hashed.update(block)
            if hashed.hexdigest() != sha:
                raise DomainError(f"文件 SHA-256 不符：{name}", 422)
        after = path.stat()

        def signature(s):
            return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

        if signature(before) != signature(after):
            raise DomainError(f"校验期间文件发生变化：{name}", 409)
        signatures.append((name, *signature(after)))
    root = confined(batch, "lerobot")
    actual = set()

    def walk_error(exc):
        raise exc

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
        for name in dirs:
            if not stat.S_ISDIR((Path(directory) / name).lstat().st_mode):
                raise DomainError("交付包含非普通目录或符号链接", 422)
        for name in names:
            path = Path(directory) / name
            actual.add(path.relative_to(batch).as_posix())
            if len(actual) > settings.max_files:
                raise DomainError("交付文件数超限", 413)
    if actual != declared:
        raise DomainError("实际文件与 files 清单不一致（缺失或未声明文件）", 422)
    return signatures


def validate_dataset(batch, data, settings, *, stopping=None):
    """Keep PyArrow/video dependencies in the existing isolated toolchain."""
    tool = executable_path(settings.letools_bin)
    python = Path(tool).with_name("python") if tool else None
    if python is None or not python.is_file():
        raise DomainError("交付校验需要安装 LeTools Python 环境（PyArrow / PyAV）", 503)
    try:
        process = subprocess.Popen(
            [
                str(python),
                str(Path(__file__).with_name("delivery_validation_worker.py")),
                str(batch),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=settings.tool_environment(),
        )
        deadline = time.monotonic() + settings.command_timeout
        payload = canonical(data).decode()
        try:
            while True:
                if stopping is not None and stopping.is_set():
                    raise DomainError("Server 停止，取消交付校验，下次扫描可重试", 503)
                if time.monotonic() >= deadline:
                    raise DomainError("交付数据结构/视频校验超时", 504)
                try:
                    stdout, stderr = process.communicate(input=payload, timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    payload = None
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
    except OSError as exc:
        raise DomainError("无法启动交付数据校验环境", 503) from exc
    if process.returncode:
        raise DomainError("LeRobot 交付校验失败：" + stderr[-2000:], 422)
    try:
        return json.loads(stdout)
    except ValueError as exc:
        raise DomainError("交付校验器未返回有效 JSON", 502) from exc
