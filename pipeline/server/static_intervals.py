"""Whole-recording static-interval detection.

Boundary trimming (``stationary_trim``) removes idle frames only at the start
and end of an episode. Whole-recording trimming removes every frame classified
as static. The worker joins the remaining motion ranges back into one
continuous output episode.

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

ALGORITHM = "static-intervals-v7"

DEFAULT_POLICY = {
    "feature": "observation.state",
    "candidate_features": ("observation.state", "state", "action"),
    "unit_thresholds": {},
    "default_threshold": DEFAULT_THRESHOLD,
    "threshold_per_frame": False,
}

POLICY_KEYS = tuple(DEFAULT_POLICY)


def normalize_policy(value: dict | None) -> dict:
    """Validate a Provider policy and return the complete runtime policy.

    Only the documented keys are accepted so a typo in a trusted Profile cannot
    silently change detection.
    """

    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise DomainError("静止帧裁剪配置必须是 JSON 对象", 400)
    unknown = set(value) - set(DEFAULT_POLICY)
    if unknown:
        raise DomainError(
            "静止帧裁剪配置包含未知字段：" + ", ".join(sorted(unknown)), 400
        )
    feature = value.get("feature", DEFAULT_POLICY["feature"])
    if not isinstance(feature, str) or not feature:
        raise DomainError("静止帧裁剪特征必须是非空字符串", 400)
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


def _active_frames(values, fps: float, thresholds, dimensions: int) -> list[bool]:
    """Classify frame motion relative to the preceding frame in the same sequence."""

    frames = len(values)
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
        active[position] = moving
    return active


def _indices_to_ranges(indices: list[int]) -> list[list[int]]:
    ranges: list[list[int]] = []
    for index in indices:
        if ranges and index == ranges[-1][1]:
            ranges[-1][1] = index + 1
        else:
            ranges.append([index, index + 1])
    return ranges


def _clean_joined_static(
    values,
    ranges: list[list[int]],
    fps: float,
    thresholds,
    dimensions: int,
) -> tuple[list[list[int]], dict]:
    """Remove static frames introduced by joining non-adjacent source ranges.

    Re-evaluate the joined numeric sequence after every removal. The new first
    frame has no predecessor and is retained as the sequence boundary; every
    later frame is active relative to its immediate predecessor. Repetition is
    finite because each changed pass removes at least one frame.
    """

    indices = [index for start, stop in ranges for index in range(start, stop)]
    initial_frames = len(indices)
    passes = []
    while len(indices) > 1:
        joined = [values[index] for index in indices]
        active = _active_frames(joined, fps, thresholds, dimensions)
        retained = [
            0,
            *(position for position in range(1, len(indices)) if active[position]),
        ]
        removed = len(indices) - len(retained)
        if not removed:
            break
        retained_set = set(retained)
        passes.append(
            {
                "pass": len(passes) + 1,
                "before_frames": len(indices),
                "after_frames": len(retained),
                "removed_frames": removed,
                "removed_source_frames": [
                    indices[position]
                    for position in range(len(indices))
                    if position not in retained_set
                ],
            }
        )
        indices = [indices[position] for position in retained]
    return _indices_to_ranges(indices), {
        "first_frame_exempt": True,
        "initial_frames": initial_frames,
        "final_frames": len(indices),
        "removed_frames": initial_frames - len(indices),
        "passes": passes,
    }


def detect_episode(values, policy: dict) -> dict:
    """Return the kept ranges for one episode.

    Frame ``i`` is "moving" when any selected dimension changes faster than its
    own threshold since frame ``i - 1``. Every static frame is removed, with no
    minimum duration, short-gap exception, padding, or join continuity rule.
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

    active = _active_frames(values, fps, thresholds, dimensions)

    static = [not moving for moving in active]
    candidates = _runs(static)
    result["static_ranges"] = [[start, stop] for start, stop in candidates]
    if not candidates:
        result["reason"] = "没有检测到静止帧"
        return result

    removals = [[start, stop] for start, stop in candidates]

    # Every motion range remains part of its source episode.

    if not removals:
        result["reason"] = "没有可删除的静止帧"
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
            reason="整段静止，默认保留并标记",
        )
        return result

    initial_keeps = keeps
    initial_kept = sum(stop - start for start, stop in initial_keeps)
    keeps, join_cleanup = _clean_joined_static(
        values, initial_keeps, fps, thresholds, dimensions
    )
    kept = sum(stop - start for start, stop in keeps)
    removed_ranges = _complement(frames, keeps)
    removed = frames - sum(stop - start for start, stop in keeps)
    result.update(
        keep_ranges=keeps,
        removed_ranges=removed_ranges,
        initial_keep_ranges=initial_keeps,
        join_cleanup=join_cleanup,
        keep_frames=kept,
        removed_frames=removed,
        output_episodes=1,
        status="trimmed",
        reason=(
            f"初始删除 {frames - initial_kept} 帧静止帧；"
            f"拼接后重复检测删除 {join_cleanup['removed_frames']} 帧；"
            "所有运动帧按原顺序前后直接相连"
        ),
    )
    return result


def summarize(episodes: list[dict], fps: float) -> dict:
    """Aggregate detection rows for the conversion summary."""

    frames = sum(row["frames"] for row in episodes)
    kept = sum(row["keep_frames"] for row in episodes)
    changed = sum(1 for row in episodes if row["removed_frames"])
    joined = sum(1 for row in episodes if len(row["keep_ranges"]) > 1)
    static = sum(1 for row in episodes if row["status"] == "static")
    return {
        "episodes": len(episodes),
        "source_episodes": len(episodes),
        # Static intervals are removed inside each source episode and the kept
        # ranges are joined, so each source episode with kept frames yields
        # exactly one output episode.
        "output_episodes": sum(1 for row in episodes if row["keep_ranges"]),
        "episodes_changed": changed,
        "episodes_joined": joined,
        "episodes_static": static,
        "episodes_unchanged": len(episodes) - changed,
        "episodes_removed": 0,
        "static_intervals": sum(len(row["removed_ranges"]) for row in episodes),
        "episodes_join_cleanup_changed": sum(
            1 for row in episodes if row.get("join_cleanup", {}).get("removed_frames")
        ),
        "join_cleanup_removed_frames": sum(
            row.get("join_cleanup", {}).get("removed_frames", 0) for row in episodes
        ),
        "join_cleanup_passes": sum(
            len(row.get("join_cleanup", {}).get("passes", [])) for row in episodes
        ),
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
