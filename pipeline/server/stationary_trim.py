"""Idle-frame boundary detection and policy validation.

A recording usually starts and ends with the robot standing still. Those frames
teach a policy to "do nothing"; removing only the *boundary* idle frames keeps
every pause inside the demonstration intact.

This module is deliberately dependency-free: the Server validates and previews
policies here, and the isolated LeTools worker imports the same functions so a
preview and the written dataset can never disagree. Robot-specific signal
semantics and thresholds come from ``RobotProvider.stationary_trim_policy``;
nothing here branches on a robot model.
"""

from __future__ import annotations

import math

from pipeline.shared.core import DomainError

ALGORITHM = "idle-boundary-v1"

# Movement thresholds are expressed per second, so they are independent of the
# recording FPS. Providers may instead declare per-frame thresholds.
DEFAULT_UNIT_THRESHOLDS = {
    "rad": 0.02,
    "radian": 0.02,
    "degrees": 1.0,
    "deg": 1.0,
    "m": 0.002,
    "cm": 0.02,
    "mm": 0.2,
    "normalized": 0.02,
    "percent": 0.5,
}
DEFAULT_THRESHOLD = 0.02
IGNORED_UNITS = {"padding", "unused"}

DEFAULT_POLICY = {
    "feature": "observation.state",
    "candidate_features": ("observation.state", "state", "action"),
    "unit_thresholds": {},
    "default_threshold": DEFAULT_THRESHOLD,
    "threshold_per_frame": False,
    "trim_start": True,
    "trim_end": True,
    "min_idle_seconds": 0.5,
    "min_active_seconds": 0.2,
    "padding_before_seconds": 0.2,
    "padding_after_seconds": 0.2,
    "remove_fully_static": False,
}

LIMITS = {
    "min_idle_seconds": (0.0, 60.0),
    "min_active_seconds": (0.0, 60.0),
    "padding_before_seconds": (0.0, 60.0),
    "padding_after_seconds": (0.0, 60.0),
}


def _boolean(value, name: str, default: bool) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise DomainError(f"{name} 必须为布尔值", 400)
    return value


def _seconds(value, name: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainError(f"{name} 必须为秒数", 400)
    value = float(value)
    low, high = LIMITS[name]
    if not math.isfinite(value) or not low <= value <= high:
        raise DomainError(f"{name} 必须在 {low}–{high} 秒之间", 400)
    return value


def _thresholds(value) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise DomainError("unit_thresholds 必须是单位到阈值的映射", 400)
    result = {}
    for unit, threshold in value.items():
        if not isinstance(unit, str) or not unit:
            raise DomainError("unit_thresholds 的单位必须是非空字符串", 400)
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise DomainError(f"单位 {unit} 的阈值必须是正数", 400)
        threshold = float(threshold)
        if not math.isfinite(threshold) or threshold <= 0:
            raise DomainError(f"单位 {unit} 的阈值必须是正数", 400)
        result[unit] = threshold
    return result


def normalize_policy(value: dict | None) -> dict:
    """Validate a Provider policy and return the complete runtime policy.

    Accepts only the keys documented in ``DEFAULT_POLICY``; an unknown key is an
    error so a typo in a trusted Profile cannot silently change detection.
    """

    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise DomainError("静止裁剪配置必须是 JSON 对象", 400)
    unknown = set(value) - set(DEFAULT_POLICY)
    if unknown:
        raise DomainError(
            "静止裁剪配置包含未知字段：" + ", ".join(sorted(unknown)), 400
        )
    feature = value.get("feature", DEFAULT_POLICY["feature"])
    if not isinstance(feature, str) or not feature:
        raise DomainError("静止裁剪特征必须是非空字符串", 400)
    candidates = value.get("candidate_features", DEFAULT_POLICY["candidate_features"])
    if not isinstance(candidates, (list, tuple)) or not all(
        isinstance(item, str) and item for item in candidates
    ):
        raise DomainError("candidate_features 必须是特征名列表", 400)
    default_threshold = value.get(
        "default_threshold", DEFAULT_POLICY["default_threshold"]
    )
    if (
        isinstance(default_threshold, bool)
        or not isinstance(default_threshold, (int, float))
        or not math.isfinite(float(default_threshold))
        or float(default_threshold) <= 0
    ):
        raise DomainError("default_threshold 必须是正数", 400)
    return {
        "feature": feature,
        "candidate_features": tuple(candidates),
        "unit_thresholds": _thresholds(value.get("unit_thresholds")),
        "default_threshold": float(default_threshold),
        "threshold_per_frame": _boolean(
            value.get("threshold_per_frame"),
            "threshold_per_frame",
            DEFAULT_POLICY["threshold_per_frame"],
        ),
        "trim_start": _boolean(
            value.get("trim_start"), "trim_start", DEFAULT_POLICY["trim_start"]
        ),
        "trim_end": _boolean(
            value.get("trim_end"), "trim_end", DEFAULT_POLICY["trim_end"]
        ),
        "min_idle_seconds": _seconds(
            value.get("min_idle_seconds"),
            "min_idle_seconds",
            DEFAULT_POLICY["min_idle_seconds"],
        ),
        "min_active_seconds": _seconds(
            value.get("min_active_seconds"),
            "min_active_seconds",
            DEFAULT_POLICY["min_active_seconds"],
        ),
        "padding_before_seconds": _seconds(
            value.get("padding_before_seconds"),
            "padding_before_seconds",
            DEFAULT_POLICY["padding_before_seconds"],
        ),
        "padding_after_seconds": _seconds(
            value.get("padding_after_seconds"),
            "padding_after_seconds",
            DEFAULT_POLICY["padding_after_seconds"],
        ),
        "remove_fully_static": _boolean(
            value.get("remove_fully_static"),
            "remove_fully_static",
            DEFAULT_POLICY["remove_fully_static"],
        ),
    }


def _dimension(shape) -> int | None:
    if not isinstance(shape, (list, tuple)) or not shape:
        return None
    dimension = 1
    for value in shape:
        if type(value) is not int or value < 1:
            return None
        dimension *= value
    return dimension


def resolve_policy(policy: dict, features: dict, fps: float) -> dict:
    """Bind a normalized policy to the dataset's declared feature.

    The chosen feature must be a declared numeric vector; its ``units`` decide
    each dimension's threshold. A dataset whose units are unknown falls back to
    ``default_threshold`` instead of guessing a robot-specific value.
    """

    if not isinstance(features, dict) or not features:
        raise DomainError("数据集缺少特征定义，无法判断静止帧", 409)
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or fps <= 0:
        raise DomainError("数据集 FPS 无效，无法判断静止帧", 409)
    declared = [policy["feature"], *policy["candidate_features"]]
    chosen = next(
        (
            name
            for name in declared
            if name in features
            and features[name].get("dtype") not in {"video", "image", "string"}
        ),
        None,
    )
    if chosen is None:
        raise DomainError(
            "数据集没有可用于静止检测的数值特征（需要 "
            + " / ".join(dict.fromkeys(declared))
            + "）",
            409,
        )
    spec = features[chosen]
    dimension = _dimension(spec.get("shape"))
    if dimension is None:
        raise DomainError(f"特征 {chosen} 缺少有效的 shape，无法判断静止帧", 409)
    units = spec.get("units")
    if not isinstance(units, list) or len(units) != dimension:
        units = ["unknown"] * dimension
    thresholds: list[float | None] = []
    for unit in units:
        unit = unit if isinstance(unit, str) else "unknown"
        if unit in IGNORED_UNITS:
            thresholds.append(None)
            continue
        value = policy["unit_thresholds"].get(
            unit, DEFAULT_UNIT_THRESHOLDS.get(unit, policy["default_threshold"])
        )
        if policy["threshold_per_frame"]:
            value = value * float(fps)
        thresholds.append(float(value))
    if not any(value is not None for value in thresholds):
        raise DomainError(f"特征 {chosen} 的所有维度都被排除，无法判断静止帧", 409)
    return {
        **policy,
        "feature": chosen,
        "dimensions": dimension,
        "units": units,
        "thresholds": thresholds,
        "fps": float(fps),
    }


def _runs(flags: list[bool], minimum: int) -> list[tuple[int, int]]:
    """Half-open spans where ``flags`` stays true for at least ``minimum``."""

    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(flags):
        if not flags[index]:
            index += 1
            continue
        stop = index + 1
        while stop < len(flags) and flags[stop]:
            stop += 1
        if stop - index >= minimum:
            spans.append((index, stop))
        index = stop
    return spans


def detect_episode(values, policy: dict) -> dict:
    """Return the boundary range kept for one episode.

    ``values`` is a frame-major sequence of numeric vectors with the same
    dimension as the resolved policy. Frame ``i`` is "active" when any selected
    dimension moves faster than its own threshold since frame ``i - 1``; the
    first frame has no predecessor and is never the sole evidence of motion.
    """

    frames = len(values)
    fps = float(policy["fps"])
    thresholds = policy["thresholds"]
    dimensions = policy["dimensions"]
    result = {
        "frames": frames,
        "keep_start": 0,
        "keep_stop": frames,
        "head_removed": 0,
        "tail_removed": 0,
        "active_start": None,
        "active_stop": None,
        "status": "unchanged",
        "reason": "",
    }
    if frames < 2:
        result.update(status="too_short", reason="帧数不足 2，无法判断静止")
        return result
    active = [False] * frames
    for position in range(1, frames):
        previous, current = values[position - 1], values[position]
        moving = False
        for dimension in range(dimensions):
            threshold = thresholds[dimension]
            if threshold is None:
                continue
            before, after = float(previous[dimension]), float(current[dimension])
            if not (math.isfinite(before) and math.isfinite(after)):
                raise ValueError(f"第 {position} 帧包含非有限数值，无法判断静止")
            if abs(after - before) * fps > threshold:
                moving = True
                break
        active[position] = moving
    minimum = max(1, round(policy["min_active_seconds"] * fps))
    spans = _runs(active, minimum)
    if not spans:
        result.update(
            status="static",
            reason=(
                "整段没有持续运动"
                if policy["remove_fully_static"]
                else "整段没有持续运动，默认保留整段"
            ),
        )
        if policy["remove_fully_static"]:
            result.update(status="removed", keep_start=frames, keep_stop=frames)
        return result
    first, last = spans[0][0], spans[-1][1]
    result.update(active_start=first, active_stop=last)
    padding_before = round(policy["padding_before_seconds"] * fps)
    padding_after = round(policy["padding_after_seconds"] * fps)
    head_idle = first
    tail_idle = frames - last
    minimum_idle = round(policy["min_idle_seconds"] * fps)
    # Idle spans are measured before padding: padding keeps extra frames around
    # the motion and must not make a long enough idle span look too short.
    keep_start = (
        max(0, first - padding_before)
        if policy["trim_start"] and head_idle >= minimum_idle
        else 0
    )
    keep_stop = (
        min(frames, last + padding_after)
        if policy["trim_end"] and tail_idle >= minimum_idle
        else frames
    )
    # Never publish a range that would drop the motion that defines it.
    keep_start = min(keep_start, first)
    keep_stop = max(keep_stop, last)
    if keep_stop - keep_start < 2:
        result.update(status="too_short", reason="可保留的帧数不足 2")
        return result
    removed = (keep_start) + (frames - keep_stop)
    result.update(
        keep_start=keep_start,
        keep_stop=keep_stop,
        head_removed=keep_start,
        tail_removed=frames - keep_stop,
    )
    if removed:
        result.update(status="trimmed", reason=f"删除首尾共 {removed} 帧静止画面")
    return result


def summarize(episodes: list[dict], fps: float) -> dict:
    """Aggregate detection rows for the preview header."""

    frames = sum(row["frames"] for row in episodes)
    kept = sum(row["keep_stop"] - row["keep_start"] for row in episodes)
    removed_episodes = sum(1 for row in episodes if row["status"] == "removed")
    trimmed = sum(1 for row in episodes if row["status"] == "trimmed")
    static = sum(1 for row in episodes if row["status"] == "static")
    return {
        "episodes": len(episodes),
        "episodes_trimmed": trimmed,
        "episodes_static": static,
        "episodes_removed": removed_episodes,
        "episodes_unchanged": len(episodes) - trimmed - static - removed_episodes,
        "frames": frames,
        "frames_kept": kept,
        "frames_removed": frames - kept,
        "duration_removed_seconds": round((frames - kept) / fps, 3) if fps else 0.0,
    }


def detect_dataset(values_by_episode: dict[int, object], policy: dict) -> dict:
    """Detect every episode and return the preview plan shape."""

    episodes: list[dict] = []
    for index in sorted(values_by_episode):
        try:
            row = detect_episode(values_by_episode[index], policy)
        except ValueError as exc:
            frames = len(values_by_episode[index])
            row = {
                "frames": frames,
                "keep_start": 0,
                "keep_stop": frames,
                "head_removed": 0,
                "tail_removed": 0,
                "active_start": None,
                "active_stop": None,
                "status": "invalid",
                "reason": str(exc),
            }
        row["episode_index"] = index
        episodes.append(row)
    return {
        "algorithm": ALGORITHM,
        "fps": policy["fps"],
        "feature": policy["feature"],
        "units": policy["units"],
        "thresholds": policy["thresholds"],
        "policy": {
            key: policy[key]
            for key in (
                "feature",
                "trim_start",
                "trim_end",
                "min_idle_seconds",
                "min_active_seconds",
                "padding_before_seconds",
                "padding_after_seconds",
                "remove_fully_static",
                "threshold_per_frame",
                "default_threshold",
                "unit_thresholds",
            )
        },
        "synthetic_fps": None,
        "episodes": episodes,
        "summary": summarize(episodes, policy["fps"]),
    }


__all__ = [
    "ALGORITHM",
    "DEFAULT_POLICY",
    "detect_dataset",
    "detect_episode",
    "normalize_policy",
    "resolve_policy",
    "summarize",
]
