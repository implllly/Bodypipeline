"""Profile-bound position relabeling; never changes measured observations.

The detector follows the position-only close/hold/release heuristic in the ARX
reference script. Only stable holding plateaus within those phases are selected.
All dimensions, motor endpoints and thresholds come from trusted server config.
NumPy is sufficient; the conversion service needs no additional toolchain.
"""

from __future__ import annotations

import math

import numpy as np

from pipeline.shared.core import DomainError

ALGORITHM = "closing_hold_plateaus_v4"
CORRECTION = "additive_shift_v1"
DEFAULT_PARAMETERS = {
    "median_frames": 5,
    "opening_prominence": 0.06,
    "minimum_closure": 0.06,
    "closing_onset": 0.02,
    "plateau_range": 0.04,
    "plateau_seconds": 0.3,
    "minimum_phase_seconds": 0.4,
    "release_window_seconds": 0.2,
    "release_drop": 0.045,
    "release_confirm_seconds": 0.5,
    "release_confirm_drop": 0.12,
    "release_onset_drop": 0.01,
    "empty_closed_margin": 0.2,
    "stability_range": 0.025,
    "minimum_plateau_seconds": 0.5,
    # Allow slow load-induced drift during a sustained grasp. Local window
    # stability and release detection still reject ramps and opening motion.
    "maximum_level_difference": 0.15,
    # Per-episode adaptation.  Amplitude thresholds keep their configured
    # values for a full nominal travel, and shrink with the travel actually
    # observed in this episode so a small-range grasp is not discarded.
    "adaptive_scale_floor": 0.25,
    # Amplitude thresholds never fall below this many robust noise sigmas.
    "noise_closure_multiple": 3.0,
    "noise_onset_multiple": 1.5,
    "noise_plateau_multiple": 6.0,
    "noise_release_multiple": 3.0,
    "noise_confirm_multiple": 8.0,
    "noise_drift_multiple": 8.0,
    # Noisy episodes need a longer look-ahead before a release is accepted.
    "maximum_confirm_scale": 2.5,
    # Release is a change point: most steps must not retrace beyond the noise
    # band, and the one-sided CUSUM must carry the decline past the noise.
    "release_monotonic_fraction": 0.7,
    "cusum_slack_multiple": 1.0,
    # Detected phases are assembled into intervals before any action is
    # written.  Short detection holes are filled and short phases dropped so a
    # single physical grasp cannot become several alternating corrections.
    "minimum_gap_seconds": 0.2,
    # Action enters and leaves the corrected level over a short ramp instead
    # of switching on a single frame boundary.
    "correction_ramp_seconds": 0.15,
}


def configuration(profile: dict) -> dict | None:
    conversion = profile.get("conversion", {})
    if not isinstance(conversion, dict):
        raise DomainError("Profile conversion 必须是对象")
    if "gripper_closure" not in conversion:
        return None
    config = conversion["gripper_closure"]
    if not isinstance(config, dict) or config.get("algorithm") != ALGORITHM:
        raise DomainError("Profile 夹爪闭合度算法未注册")
    if (
        not isinstance(config.get("calibration_id"), str)
        or not config["calibration_id"]
    ):
        raise DomainError("Profile 夹爪闭合度配置缺少 calibration_id")
    names = profile.get("joint_names", [])
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise DomainError("Profile 夹爪通道需要 joint_names 字符串列表")
    channels = config.get("channels")
    if not isinstance(channels, list) or not channels:
        raise DomainError("Profile 夹爪闭合度配置缺少 channels")
    seen = set()
    for channel in channels:
        if not isinstance(channel, dict):
            raise DomainError("Profile 夹爪通道必须是对象")
        joint = channel.get("joint")
        if not isinstance(joint, str) or names.count(joint) != 1 or joint in seen:
            raise DomainError("Profile 夹爪通道必须绑定唯一的已声明关节")
        seen.add(joint)
        group = channel.get("group")
        if group is not None and (not isinstance(group, str) or not group):
            raise DomainError("Profile 夹爪通道 group 必须是非空字符串")
        endpoints = [channel.get("open"), channel.get("closed")]
        if any(
            type(v) not in (int, float)
            or not math.isfinite(v)
            or abs(v) > np.finfo(np.float32).max
            for v in endpoints
        ):
            raise DomainError("Profile 夹爪行程端点必须是有限数值")
        if endpoints[0] == endpoints[1]:
            raise DomainError("Profile 夹爪全开与全闭端点不能相同")
    supplied = config.get("phase_parameters", {})
    if not isinstance(supplied, dict) or supplied.keys() - DEFAULT_PARAMETERS.keys():
        raise DomainError("Profile 夹爪区间识别参数无效")
    params = {**DEFAULT_PARAMETERS, **supplied}
    for key, value in params.items():
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise DomainError(f"Profile 夹爪参数 {key} 必须是正有限数值")
    if params["adaptive_scale_floor"] > 1:
        raise DomainError("Profile 夹爪参数 adaptive_scale_floor 不能超过 1")
    if params["release_monotonic_fraction"] > 1:
        raise DomainError("Profile 夹爪参数 release_monotonic_fraction 不能超过 1")
    if params["maximum_confirm_scale"] < 1:
        raise DomainError("Profile 夹爪参数 maximum_confirm_scale 不能小于 1")
    median = params["median_frames"]
    if type(median) is not int or median % 2 != 1 or median > 101:
        raise DomainError("Profile median_frames 必须为不超过 101 的正奇数")
    fps = profile.get("output_fps")
    if type(fps) is not int or not 1 <= fps <= 240:
        raise DomainError("Profile 夹爪修正需要有效的 output_fps")
    default = config.get("default_increase_percent", 10)
    if (
        type(default) not in (float, int)
        or not math.isfinite(default)
        or not 0 <= default <= 100
    ):
        raise DomainError("Profile 夹爪默认增加闭合度必须在 0–100 之间")
    return {**config, "phase_parameters": params, "default_increase_percent": default}


def capabilities(profile: dict) -> dict:
    config = configuration(profile)
    if config is None:
        return {}
    return {
        "gripper_closure": {
            "correction": CORRECTION,
            "minimum_increase_percent": 0,
            "maximum_increase_percent": 100,
            "default_increase_percent": config["default_increase_percent"],
            "algorithm": config["algorithm"],
            "calibration_id": config["calibration_id"],
            "percentage_definition": "nominal_motor_travel",
        }
    }


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    edges = np.diff(np.r_[False, mask, False].astype(np.int8))
    return list(
        zip(np.flatnonzero(edges == 1).tolist(), np.flatnonzero(edges == -1).tolist())
    )


def _troughs(x: np.ndarray, prominence: float) -> list[int]:
    """Local troughs whose rise to both sides reaches the configured prominence.

    Collapse equal-valued runs before finding extrema, including flat troughs.
    Stop each search at a lower trough, as in topographic prominence. We do not
    need SciPy's general peak width/distance machinery for this detector.
    """
    starts = np.r_[0, np.flatnonzero(np.diff(x) != 0) + 1]
    levels = x[starts]
    candidates = (
        np.flatnonzero((levels[1:-1] < levels[:-2]) & (levels[1:-1] < levels[2:])) + 1
    )
    result = []
    for k in candidates:
        level = levels[k]
        left = k - 1
        while left > 0 and levels[left] >= level and levels[left] < level + prominence:
            left -= 1
        right = k + 1
        while (
            right < len(levels) - 1
            and levels[right] >= level
            and levels[right] < level + prominence
        ):
            right += 1
        if levels[left] >= level + prominence and levels[right] >= level + prominence:
            stop = starts[k + 1] if k + 1 < len(starts) else len(x)
            result.append(int((starts[k] + stop - 1) // 2))
    return result


def _noise_band(position: np.ndarray) -> float:
    """Robust per-frame noise sigma from the median absolute difference.

    Piecewise-constant holds and constant-velocity ramps both have a single
    dominant difference, so the MAD around it ignores the motion itself and
    only keeps the jitter.  For white noise sigma the first difference has
    standard deviation ``sqrt(2) * sigma``.
    """
    if len(position) < 3:
        return 0.0
    differences = np.diff(position)
    mad = np.median(np.abs(differences - np.median(differences)))
    return float(1.4826 * mad / math.sqrt(2.0))


def _effective_parameters(
    params: dict, x: np.ndarray, noise: float, closed: float, opened: float | None
) -> tuple[dict, dict]:
    """Scale configured thresholds to this episode's travel and noise.

    The configured amplitudes describe a full nominal travel.  ``scale`` is the
    fraction of that travel actually observed here, floored so a deliberately
    narrow grasp can still be recognised.  Every amplitude is then bounded
    below by a multiple of the robust noise sigma, which prevents a noisy
    episode from entering HOLDING on jitter alone.
    """
    low, high = np.percentile(x, [1, 99])
    span = float(high - low)
    travel = abs(closed - opened) if opened is not None else 0.0
    if travel > 0:
        scale = min(1.0, max(params["adaptive_scale_floor"], span / travel))
    else:
        scale = 1.0
    closure = max(
        params["minimum_closure"] * scale, params["noise_closure_multiple"] * noise
    )
    onset = max(params["closing_onset"] * scale, params["noise_onset_multiple"] * noise)
    plateau = max(
        params["plateau_range"] * scale, params["noise_plateau_multiple"] * noise
    )
    stability = max(
        params["stability_range"] * scale, params["noise_plateau_multiple"] * noise
    )
    drop = max(params["release_drop"] * scale, params["noise_release_multiple"] * noise)
    confirm_drop = max(
        params["release_confirm_drop"] * scale, params["noise_confirm_multiple"] * noise
    )
    onset_drop = max(
        params["release_onset_drop"] * scale, params["noise_onset_multiple"] * noise
    )
    # Widen the confirm window against the travel-scaled closure, not the
    # noise-raised one, so heavy jitter actually buys more look-ahead.
    base_closure = params["minimum_closure"] * scale
    widening = 1.0 + noise / base_closure if base_closure > 0 else 1.0
    confirm_seconds = params["release_confirm_seconds"] * min(
        params["maximum_confirm_scale"], max(1.0, widening)
    )
    effective = {
        "opening_prominence": max(
            params["opening_prominence"] * scale,
            params["noise_closure_multiple"] * noise,
        ),
        "minimum_closure": closure,
        "closing_onset": onset,
        "plateau_range": plateau,
        "stability_range": stability,
        "release_drop": drop,
        "release_confirm_drop": confirm_drop,
        "release_onset_drop": onset_drop,
        "release_confirm_seconds": confirm_seconds,
        # A sustained grasp may still drift; keep the configured allowance but
        # never below the noise band, or a noisy hold never validates.
        "maximum_level_difference": max(
            params["maximum_level_difference"],
            params["noise_drift_multiple"] * noise,
        ),
        # A decline step must clear the noise band before it accumulates.
        "cusum_slack": params["cusum_slack_multiple"] * noise,
    }
    diagnostics = {
        "observed_span": span,
        "nominal_travel": travel,
        "noise_band": noise,
        "scale": scale,
        "effective_parameters": effective,
        # Final phase intervals are filled in by ``detect``; short sequences
        # that return early still need the keys so callers can always build an
        # action weight from them.
        "intervals": [],
        "raw_interval_count": 0,
        "filled_gap_count": 0,
        "dropped_interval_count": 0,
    }
    return effective, diagnostics


def _release_stop(
    x: np.ndarray,
    scan: int,
    effective: dict,
    params: dict,
    release_n: int,
    confirm_n: int,
) -> int:
    """Return where HOLDING ends, using a monotonic change point for release.

    A release must look ahead over the confirm window and satisfy all of:
    a sustained drop, mostly non-retracing steps, and a one-sided CUSUM that
    carries the decline past the noise band.  Fixed-window amplitude voting
    alone re-triggers on jitter; the CUSUM lets a slow ramp accumulate while
    noise around a flat hold does not.

    The drop over ``release_n`` is an average-rate test: it rejects a holding
    plateau whose filtered slope is orders of magnitude smaller, while a real
    release clears it within a fraction of a second.  A hold never ends just
    because the measured position reached the nominal empty-close level: a
    shallow grasp can sit on that level while the gripper is still firmly
    closed, so the closing motion is long over while the level stays there.
    Reaching the empty level is rejected at entry instead; only a confirmed
    downward change point ends the hold.
    """
    n = len(x)
    slack = effective["cusum_slack"]
    for k in range(scan, n):
        look = x[k : min(k + confirm_n + 1, n)]
        if len(look) < release_n + 1:
            continue
        lowest = int(np.argmin(look))
        if lowest < release_n:
            continue
        # Use the robust level at the candidate frame so one noisy sample
        # cannot shift the release onset by several frames.
        baseline = float(np.median(look[:release_n]))
        # The first release window must move further than the fast-drop
        # threshold; slow load drift never qualifies on its own.
        if look[release_n] - baseline > -effective["release_drop"]:
            continue
        if baseline - look[lowest] < effective["release_confirm_drop"]:
            continue
        steps = -np.diff(look[: lowest + 1])
        declining = int(np.count_nonzero(steps >= -slack))
        if declining / lowest < params["release_monotonic_fraction"]:
            continue
        level = peak = 0.0
        for i in range(1, lowest + 1):
            level = max(0.0, level + (look[i - 1] - look[i]) - slack)
            peak = max(peak, level)
        if peak < effective["release_drop"]:
            continue
        onset = np.flatnonzero(look < baseline - effective["release_onset_drop"])
        return k + int(onset[0]) if len(onset) else k
    return n


def _valley_level(x: np.ndarray, index: int, band: float, half: int) -> float:
    """Robust open level around a trough, ignoring the rising sides.

    A single noisy sample at frame zero must not make the whole open plateau
    look like a small grasp, so the level is the median of nearby samples
    within one amplitude band of the trough.
    """
    low, high = max(0, index - half), min(len(x), index + half + 1)
    near = x[low:high]
    near = near[near <= x[index] + band]
    return float(np.median(near)) if len(near) else float(x[index])


def _assemble_intervals(
    intervals: list[tuple[int, int]], gap_n: int, minimum_n: int
) -> tuple[list[tuple[int, int]], int, int]:
    """Merge detection fragments into one interval per physical phase.

    A short detection hole inside a grasp must not become an action gap, and a
    phase shorter than the minimum duration is discarded instead of being
    written as a one-frame spike.
    """
    ordered = sorted((int(a), int(b)) for a, b in intervals if b > a)
    merged: list[tuple[int, int]] = []
    gaps = 0
    for start, end in ordered:
        if merged and start <= merged[-1][1] + gap_n:
            if start > merged[-1][1]:
                gaps += 1
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    kept = [(a, b) for a, b in merged if b - a >= minimum_n]
    return kept, gaps, len(merged) - len(kept)


def _smoothstep(value):
    value = np.clip(value, 0.0, 1.0)
    return value * value * (3.0 - 2.0 * value)


def _action_weight(
    intervals: list[tuple[int, int]],
    length: int,
    offset: int,
    ramp_n: int,
) -> np.ndarray:
    """Map observation intervals to action frames with a smooth boundary.

    ``offset`` is the Provider's action/observation contract: action frame
    ``t`` observes state frame ``t + offset``.  DAgger records the next-frame
    position, so its correction must be shifted by one frame.
    """
    weight = np.zeros(length, dtype=np.float64)
    for observation_start, observation_end in intervals:
        start = max(0, int(observation_start) - offset)
        end = min(length, int(observation_end) - offset)
        if start >= end:
            continue
        span = end - start
        ramp = max(1, min(ramp_n, span // 2 if span > 1 else 1))
        for frame in range(start, end):
            # Start from zero at the first corrected frame and approach zero at
            # the exit, so the corrected action stays continuous with the
            # frames before and after the phase.
            enter = (frame - start) / ramp
            leave = (end - frame) / ramp
            alpha = float(min(_smoothstep(enter), _smoothstep(leave), 1.0))
            weight[frame] = max(weight[frame], alpha)
    return weight


def detect(
    position: np.ndarray,
    fps: float,
    closed: float,
    params: dict,
    *,
    opened: float | None = None,
) -> tuple[np.ndarray, dict]:
    """Return the HOLDING state and the per-episode adaptive diagnostics.

    Input increases toward closed.  ``position`` is measured feedback
    (``observation.state``), never the command channel: the action is only
    rewritten once a grasp has been evidenced by the observation.
    """
    raw = np.asarray(position, dtype=np.float64)
    width = params["median_frames"]
    padded = np.pad(raw, width // 2, mode="edge")
    x = np.median(np.lib.stride_tricks.sliding_window_view(padded, width), axis=1)
    # Thresholds operate on the median-filtered signal.  A median of ``width``
    # iid samples scales sigma by about sqrt(pi / (2 * width)); estimate the
    # noise on the raw feedback and project it through that filter.
    filtered_noise = _noise_band(raw) * 1.2533 / math.sqrt(width)
    n = len(x)
    raw_intervals: list[tuple[int, int]] = []
    effective, diagnostics = _effective_parameters(
        params, x, filtered_noise, closed, opened
    )
    gap_n = max(1, round(params["minimum_gap_seconds"] * fps))
    stable_n = max(2, round(params["plateau_seconds"] * fps))
    minimum_n = max(1, round(params["minimum_phase_seconds"] * fps))
    plateau_n = max(1, round(params["minimum_plateau_seconds"] * fps))
    release_n = max(1, round(params["release_window_seconds"] * fps))
    confirm_n = max(release_n, round(effective["release_confirm_seconds"] * fps))
    if n < max(stable_n, minimum_n, plateau_n):
        return np.zeros(n, dtype=bool), diagnostics
    empty_closed = closed - params["empty_closed_margin"]
    band = max(effective["plateau_range"], effective["minimum_closure"])
    valleys = _troughs(x, effective["opening_prominence"])
    if x[0] < empty_closed:
        valleys = sorted(set([0, *valleys]))
    windows = np.lib.stride_tricks.sliding_window_view(x, stable_n)
    ranges, medians = np.ptp(windows, axis=1), np.median(windows, axis=1)
    for j, valley in enumerate(valleys):
        level = _valley_level(x, valley, band, stable_n)
        end = valleys[j + 1] if j + 1 < len(valleys) else n
        hits = np.flatnonzero(x[valley:end] >= level + effective["closing_onset"])
        if not len(hits):
            continue
        start = valley + int(hits[0])
        candidates = np.flatnonzero(
            (
                ranges[start : max(start, end - stable_n + 1)]
                <= effective["plateau_range"]
            )
            & (
                medians[start : max(start, end - stable_n + 1)]
                >= level + effective["minimum_closure"]
            )
            & (medians[start : max(start, end - stable_n + 1)] < empty_closed)
        )
        if not len(candidates):
            continue
        stable = start + int(candidates[0])
        if np.any(x[start:stable] >= empty_closed):
            continue
        # Once a real closing movement has been confirmed by a later stable
        # plateau, enter HOLDING at the rising edge, not at the first quiet
        # window.  This is deliberately a state-machine boundary: every
        # action frame from closing onset through release is held at the
        # requested minimum closure.  The old window-only implementation left
        # the original action on the closing ramp and at small gaps in the
        # plateau, which could create an artificial drop in an otherwise
        # continuous hold.
        stop = _release_stop(
            x, stable + stable_n, effective, params, release_n, confirm_n
        )
        if stop - start < minimum_n:
            continue
        # First validate that the phase really contains a stable platform.
        # The platform is only the evidence needed to enter HOLDING; once
        # entered, the state extends from ``start`` until the confirmed
        # release.  Motion outside that interval retains its original action.
        eligible = np.zeros(n, dtype=bool)
        for k in range(stable, min(stop - stable_n + 1, len(ranges))):
            if ranges[k] <= effective["stability_range"] and medians[k] < empty_closed:
                eligible[k : k + stable_n] = True
        valid_plateau = any(
            b - a >= plateau_n
            and np.ptp(x[a:b]) <= effective["maximum_level_difference"]
            for a, b in _runs(eligible)
        )
        if valid_plateau and stop - start >= minimum_n:
            raw_intervals.append((start, min(stop, n)))
    intervals, filled_gaps, dropped = _assemble_intervals(
        raw_intervals, gap_n, minimum_n
    )
    mask = np.zeros(n, dtype=bool)
    for start, stop in intervals:
        mask[start:stop] = True
    diagnostics["intervals"] = [[start, stop] for start, stop in intervals]
    diagnostics["raw_interval_count"] = len(raw_intervals)
    diagnostics["filled_gap_count"] = filled_gaps
    diagnostics["dropped_interval_count"] = dropped
    return mask, diagnostics


def holding_mask(
    position: np.ndarray, fps: float, closed: float, params: dict
) -> np.ndarray:
    """Return only the HOLDING state; see :func:`detect` for diagnostics."""
    return detect(position, fps, closed, params)[0]


def _channel_entry(profile: dict, channel: dict):
    joint = channel["joint"]
    dimension = profile["joint_names"].index(joint)
    opened, closed = channel["open"], channel["closed"]
    direction = 1 if closed > opened else -1
    return {
        "channel": channel,
        "joint": joint,
        "dimension": dimension,
        "open": opened,
        "closed": closed,
        "direction": direction,
        "open_adj": opened * direction,
        "closed_adj": closed * direction,
    }


def _fuse_progress(state: np.ndarray, entries: list[dict]) -> np.ndarray | None:
    """Median normalized closure across channels configured in one group.

    Channels stay independent unless the Profile explicitly puts them in the
    same ``group``: a dual-arm robot has two independent grippers, while two
    fingers of one gripper should share one phase.
    """
    if len(entries) < 2:
        return None
    stack = []
    for entry in entries:
        opened = entry["open_adj"]
        section = entry["closed_adj"] - opened
        if section == 0:
            return None
        stack.append(
            (state[:, entry["dimension"]] * entry["direction"] - opened) / section
        )
    return np.median(np.vstack(stack), axis=0)


def _closure_progress(values: np.ndarray, opened: float, closed: float) -> np.ndarray:
    """Normalized nominal travel: 0 at open, 1 at fully closed."""
    return (values - opened) / (closed - opened)


def correct_actions(
    state: np.ndarray,
    action: np.ndarray,
    profile: dict,
    options: dict,
    *,
    action_state_offset: int = 0,
) -> tuple[np.ndarray, dict]:
    config = configuration(profile)
    if config is None:
        raise DomainError("所选 Profile 不支持夹爪闭合度修正", 400)
    if type(action_state_offset) is not int:
        raise DomainError("夹爪 action/state 时间偏移必须是整数")
    increase = options["increase_percent"]
    requested = increase / 100.0
    revised = action.copy()
    any_changed = np.zeros(len(action), dtype=bool)
    limited_any = False
    parameters = config["phase_parameters"]
    ramp_n = max(
        1, round(parameters["correction_ramp_seconds"] * profile["output_fps"])
    )
    groups: dict[str, list[dict]] = {}
    for channel in config["channels"]:
        entry = _channel_entry(profile, channel)
        groups.setdefault(channel.get("group") or entry["joint"], []).append(entry)
    channels = {}
    for name, entries in groups.items():
        first = entries[0]
        fused = _fuse_progress(state, entries)
        if fused is None:
            position = state[:, first["dimension"]] * first["direction"]
            source = [first]
        else:
            position = first["open_adj"] + fused * (
                first["closed_adj"] - first["open_adj"]
            )
            source = entries
        mask, adaptive = detect(
            position,
            profile["output_fps"],
            first["closed_adj"],
            parameters,
            opened=first["open_adj"],
        )
        if len(entries) > 1:
            adaptive["group"] = name
            adaptive["fused_channels"] = [entry["joint"] for entry in entries]
        weight = _action_weight(
            [tuple(pair) for pair in adaptive["intervals"]],
            len(action),
            action_state_offset,
            ramp_n,
        )
        action_mask = weight > 0
        # One constant offset per physical grasp.  Channels in the same group
        # must move by the same normalized amount so their relative positions
        # stay unchanged, and the offset is reduced (never truncated per frame)
        # when any channel is already close to fully closed.  This keeps the
        # original curve shape inside the interval instead of flattening it.
        shifts = {
            entry["joint"]: np.zeros(len(action), dtype=np.float64)
            for entry in source
        }
        applied_intervals = []
        for start, stop in _runs(action_mask):
            peak = max(
                float(
                    np.max(
                        _closure_progress(
                            action[start:stop, entry["dimension"]],
                            entry["open"],
                            entry["closed"],
                        )
                    )
                )
                for entry in source
            )
            allowed = max(0.0, 1.0 - peak)
            applied = min(requested, allowed)
            limited = applied + 1e-12 < requested
            limited_any = limited_any or limited
            applied_intervals.append(
                {
                    "from_frame": start,
                    "to_frame_exclusive": stop,
                    "requested_increase_percent": increase,
                    "applied_increase_percent": applied * 100,
                    "max_original_closure_percent": peak * 100,
                    "limited_by_travel": limited,
                }
            )
            for entry in source:
                shifts[entry["joint"]][start:stop] = weight[start:stop] * applied
        for entry in source:
            dimension = entry["dimension"]
            opened, closed = entry["open"], entry["closed"]
            values = revised[:, dimension]
            values += shifts[entry["joint"]] * (closed - opened)
            changed = values != action[:, dimension]
            any_changed |= changed
            channels[entry["joint"]] = {
                "dimension": dimension,
                "open": opened,
                "closed": closed,
                "increase_percent": increase,
                "applied_intervals": applied_intervals,
                "evidence": "observation.state",
                "action_state_offset": action_state_offset,
                "group": name if len(entries) > 1 else None,
                "adaptive": adaptive,
                "selected_frames": int(action_mask.sum()),
                "ramped_frames": int(np.count_nonzero((weight > 0) & (weight < 1))),
                "full_frames": int(np.count_nonzero(weight == 1)),
                "changed_frames": int(changed.sum()),
                "selected_intervals": [
                    {"from_frame": a, "to_frame_exclusive": b}
                    for a, b in _runs(action_mask)
                ],
                "changed_intervals": [
                    {"from_frame": a, "to_frame_exclusive": b}
                    for a, b in _runs(changed)
                ],
            }
    warnings = []
    if not any_changed.any():
        if limited_any:
            warnings.append("夹持区间已接近名义全闭，无法继续增加闭合度；动作保持原值")
        else:
            warnings.append("未找到需要提高闭合度的稳定夹持帧；动作保持原值")
    elif limited_any:
        warnings.append("部分夹持区间已接近名义全闭，增量按区间峰值缩小")
    return revised, {
        "algorithm": ALGORITHM,
        "correction": CORRECTION,
        "calibration_id": config["calibration_id"],
        "increase_percent": increase,
        "percentage_definition": "nominal_motor_travel; not aperture or force",
        "evidence_channel": "observation.state",
        "action_state_offset": action_state_offset,
        "phase_parameters": parameters,
        "changed_features": ["action"],
        "changed_frames_any_channel": int(any_changed.sum()),
        "channels": channels,
        "warnings": warnings,
    }
