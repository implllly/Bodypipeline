"""Trusted, robot-independent configuration for immutable dataset deliveries."""

import math
from pathlib import Path


def validate_delivery_config(settings):
    import re

    for name in ("delivery_scan_seconds", "delivery_retry_seconds"):
        value = getattr(settings, name)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 1:
            raise ValueError(f"{name} 必须为至少 1 秒的有限数")
    if type(settings.delivery_scan_batch_limit) is not int or not (
        1 <= settings.delivery_scan_batch_limit <= 100
    ):
        raise ValueError("delivery_scan_batch_limit 应为 1–100 的整数")
    if not isinstance(settings.delivery_sources, list):
        raise ValueError("delivery_sources 必须为列表")
    roots = [Path(p).expanduser().resolve() for p in settings.import_roots]
    if settings.data_root is not None:
        roots.append(Path(settings.data_root).expanduser().resolve())
    seen = []
    for source in settings.delivery_sources:
        if not isinstance(source, dict) or set(source) != {"root", "robot_type"}:
            raise ValueError("delivery_sources 每项只允许 root 和 robot_type")
        raw, robot = source["root"], source["robot_type"]
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            raise ValueError("delivery source root 必须为绝对路径")
        path = Path(raw)
        if path.resolve() != path:
            raise ValueError("delivery source root 不得含符号链接或非规范路径")
        if not any(path == root or path.is_relative_to(root) for root in roots):
            raise ValueError(
                "delivery source root 必须位于 import_roots 或 data_root 内"
            )
        if not isinstance(robot, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", robot
        ):
            raise ValueError("delivery source robot_type 无效")
        if path.name != robot:
            raise ValueError("delivery source root 末级目录必须等于 robot_type")
        if any(
            path == p or path.is_relative_to(p) or p.is_relative_to(path) for p in seen
        ):
            raise ValueError("delivery source roots 不得重复或重叠")
        seen.append(path)
