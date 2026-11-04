"""Whole-recording static-interval detection.

Boundary trimming (``stationary_trim``) removes idle frames only at the start
and end of an episode. A long recording may also contain sustained static spans
in the middle: an operator waiting for a policy result, a pause after a failed
grasp, or a stretch where the robot is simply held still. This module finds
those spans anywhere in the recording and returns the ranges to keep, so the
worker can split one source episode into several output episodes.

The module is dependency-free and never branches on a robot model: signal
semantics and thresholds come from ``RobotProvider.stationary_interval_policy``.
Detection intentionally reports "static interval", not "inference pause": the
motion signal cannot tell why the robot stopped.
"""

from __future__ import annotations

import math

from pipeline.shared.core import DomainError

from .stationary_trim import (
    DEFAULT_THRESHOLD,
    DEFAULT_UNIT_THRESHOLDS,
    IGNORED_UNITS,
    _boolean,
    _thresholds,
    resolve_policy,
)

ALGORITHM = "static-intervals-v1"

DEFAULT_POLICY = {
    "feature": "observation.state",
    "candidate_features": ("observation.state", "state", "action"),
    "unit_thresholds": {},
    "default_threshold": DEFAULT_THRESHOLD,
    "threshold_per_frame": False,
    "min_static_seconds": 2.0,
    "min_motion_seconds": 0.2,
    "jitter_amplitude_seconds": 0.2,
    "padding_seconds": 0.2,
    "min_output_seconds": 1.0,
}

LIMITS = {
    "min_static_seconds": (0.0, 600.0),
    "min_motion_seconds": (0.0, 600.0),
    "jitter_amplitude_seconds": (0.0, 60.0),
    "padding_seconds": (0.0, 60.0),
    "min_output_seconds": (0.0, 600.0),
}

POLICY_KEYS = tuple(DEFAULT_POLICY)


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


def normalize_policy(value: dict | None) -> dict:
    """Validate a Provider policy and return the complete runtime policy.

    Only the documented keys are accepted so a typo in a trusted Profile cannot
    silently change detection.
    """

    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise DomainError("全程静止区间配置必须是 JSON 对象", 400)
    unknown = set(value) - set(DEFAULT_POLICY)
    if unknown:
        raise DomainError(
            "全程静止区间配置包含未知字段：" + ", ".join(sorted(unknown)), 400
        )
    feature = value.get("feature", DEFAULT_POLICY["feature"])
    if not isinstance(feature, str) or not feature:
        raise DomainError("全程静止区间特征必须是非空字符串", 400)
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
        "min_static_seconds": _seconds(
            value.get("min_static_seconds"),
            "min_static_seconds",
            DEFAULT_POLICY["min_static_seconds"],
        ),
        "min_motion_seconds": _seconds(
            value.get("min_motion_seconds"),
            "min_motion_seconds",
            DEFAULT_POLICY["min_motion_seconds"],
        ),
        "jitter_amplitude_seconds": _seconds(
            value.get("jitter_amplitude_seconds"),
            "jitter_amplitude_seconds",
            DEFAULT_POLICY["jitter_amplitude_seconds"],
        ),
        "padding_seconds": _seconds(
            value.get("padding_seconds"),
            "padding_seconds",
            DEFAULT_POLICY["padding_seconds"],
        ),
        "min_output_seconds": _seconds(
            value.get("min_output_seconds"),
            "min_output_seconds",
            DEFAULT_POLICY["min_output_seconds"],
        ),
    }


def _runs(flags: list[bool]) -> list[tuple[int, int]]:
    """Half-open spans where ``flags`` stays true."""

    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(flags):
        if not flags[index]:
            index += 1
            continue
        stop = index + 1
        while stop < len(flags) and flags[stop]:
            stop += 1
        spans.append((index, stop))
        index = stop
    return spans


def _jitter(
    values, start: int, stop: int, thresholds, fps: float, amplitude_seconds: float
) -> bool:
    """Whether a short motion burst is indistinguishable from sensor jitter.

    Duration alone is not enough: a brief but large movement is a real action.
    The burst is jitter only when its net displacement stays within
    ``threshold × amplitude_seconds`` on every selected dimension.
    """

    before, after = values[start - 1], values[stop - 1]
    for dimension, threshold in enumerate(thresholds):
        if threshold is None:
            continue
        limit = threshold * amplitude_seconds
        if abs(float(after[dimension]) - float(before[dimension])) > limit:
            return False
    return True


def _complement(frames: int, removals: list[list[int]]) -> list[list[int]]:
    keeps: list[list[int]] = []
    cursor = 0
    for start, stop in removals:
        if start > cursor:
            keeps.append([cursor, start])
        cursor = stop
    if cursor < frames:
        keeps.append([cursor, frames])
    return keeps


def detect_episode(values, policy: dict) -> dict:
    """Return the kept ranges for one episode.

    Frame ``i`` is "moving" when any selected dimension changes faster than its
    own threshold since frame ``i - 1``. Short bursts that stay within a small
    displacement are ignored, so sensor jitter cannot split a long static span.
    Every detected span is shrunk by ``padding_seconds`` at each side that has a
    neighbouring motion, then the complement becomes the output episodes.
    """

    frames = len(values)
    fps = float(policy["fps"])
    thresholds = policy["thresholds"]
    dimensions = policy["dimensions"]
    result = {
        "frames": frames,
        "keep_ranges": [[0, frames]] if frames else [],
        "removed_ranges": [],
        "static_ranges": [],
        "keep_frames": frames,
        "removed_frames": 0,
        "output_episodes": 1,
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

    static = [True] + [not active[position] for position in range(1, frames)]
    minimum_motion = max(1, round(policy["min_motion_seconds"] * fps))
    for start, stop in _runs(active):
        if stop - start < minimum_motion and _jitter(
            values,
            start,
            stop,
            thresholds,
            fps,
            policy["jitter_amplitude_seconds"],
        ):
            for position in range(start, stop):
                static[position] = True

    minimum_static = max(1, round(policy["min_static_seconds"] * fps))
    candidates = [
        (start, stop) for start, stop in _runs(static) if stop - start >= minimum_static
    ]
    result["static_ranges"] = [[start, stop] for start, stop in candidates]
    if not candidates:
        result["reason"] = "没有持续达到门槛的静止区间"
        return result

    padding = round(policy["padding_seconds"] * fps)
    removals: list[list[int]] = []
    for start, stop in candidates:
        # Keep padding only next to real motion; the recording boundary needs no
        # context frames because nothing precedes or follows it.
        cut_start = start if start == 0 else start + padding
        cut_stop = stop if stop == frames else stop - padding
        if cut_stop - cut_start >= 1:
            removals.append([cut_start, cut_stop])
    if not removals:
        result["reason"] = "静止区间保留边界余量后不足 1 帧"
        return result

    # A kept span shorter than the declared minimum is not publishable. Cancel
    # the neighbouring removal instead of silently dropping or keeping a stub.
    minimum_output = max(1, round(policy["min_output_seconds"] * fps))
    while True:
        keeps = _complement(frames, removals)
        short = next(
            (
                index
                for index, (start, stop) in enumerate(keeps)
                if stop - start < minimum_output
            ),
            None,
        )
        if short is None or not removals:
            break
        options = []
        if short - 1 >= 0:
            options.append(short - 1)
        if short < len(removals):
            options.append(short)
        if not options:
            break
        chosen = max(
            options,
            key=lambda index: (
                removals[index][1] - removals[index][0],
                -index,
            ),
        )
        removals.pop(chosen)

    if not removals:
        result["reason"] = "没有可删除的静止区间"
        return result

    keeps = _complement(frames, removals)
    if not keeps:
        # A fully static episode is kept and marked: a conversion must not make
        # a whole recording disappear because the robot never moved.
        result.update(
            status="static",
            keep_ranges=[[0, frames]],
            keep_frames=frames,
            removed_ranges=[],
            removed_frames=0,
            output_episodes=1,
            reason="整段持续静止，默认保留并标记",
        )
        return result

    kept = sum(stop - start for start, stop in keeps)
    removed = frames - kept
    result.update(
        keep_ranges=keeps,
        removed_ranges=removals,
        keep_frames=kept,
        removed_frames=removed,
        output_episodes=len(keeps),
        status="split" if len(keeps) > 1 else "trimmed",
        reason=f"删除 {len(removals)} 段全程静止区间，共 {removed} 帧",
    )
    return result


def summarize(episodes: list[dict], fps: float) -> dict:
    """Aggregate detection rows for the conversion summary."""

    frames = sum(row["frames"] for row in episodes)
    kept = sum(row["keep_frames"] for row in episodes)
    changed = sum(1 for row in episodes if row["removed_frames"])
    split = sum(1 for row in episodes if len(row["keep_ranges"]) > 1)
    static = sum(1 for row in episodes if row["status"] == "static")
    return {
        "episodes": len(episodes),
        "source_episodes": len(episodes),
        "output_episodes": sum(len(row["keep_ranges"]) for row in episodes),
        "episodes_changed": changed,
        "episodes_split": split,
        "episodes_static": static,
        "episodes_unchanged": len(episodes) - changed,
        "episodes_removed": 0,
        "static_intervals": sum(len(row["removed_ranges"]) for row in episodes),
        "frames": frames,
        "frames_kept": kept,
        "frames_removed": frames - kept,
        "duration_removed_seconds": round((frames - kept) / fps, 3) if fps else 0.0,
    }


def detect_dataset(values_by_episode: dict[int, object], policy: dict) -> dict:
    """Detect every episode and return the conversion plan shape."""

    episodes: list[dict] = []
    for index in sorted(values_by_episode):
        values = values_by_episode[index]
        try:
            row = detect_episode(values, policy)
        except ValueError as exc:
            frames = len(values)
            row = {
                "frames": frames,
                "keep_ranges": [[0, frames]] if frames else [],
                "removed_ranges": [],
                "static_ranges": [],
                "keep_frames": frames,
                "removed_frames": 0,
                "output_episodes": 1,
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
        "policy": {key: policy[key] for key in POLICY_KEYS},
        "episodes": episodes,
        "summary": summarize(episodes, policy["fps"]),
    }


__all__ = [
    "ALGORITHM",
    "DEFAULT_POLICY",
    "DEFAULT_THRESHOLD",
    "DEFAULT_UNIT_THRESHOLDS",
    "IGNORED_UNITS",
    "POLICY_KEYS",
    "detect_dataset",
    "detect_episode",
    "normalize_policy",
    "resolve_policy",
    "summarize",
]
