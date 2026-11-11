"""Conversion options, position relabeling, and immutable source regression tests."""

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import h5py
import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_aloha_hdf5_import import hdf5_settings, profile, write_episode
from test_conversion_workbench import seed_upload

from pipeline.server.app import create_app
from pipeline.server.conversion_options import normalize_options
from pipeline.server.jobs import JobRunner
from pipeline.server.providers.aloha_hdf5 import AlohaHDF5Provider
from pipeline.server.providers.gripper_closure import (
    DEFAULT_PARAMETERS,
    _assemble_intervals,
    capabilities,
    correct_actions,
    detect,
    holding_mask,
)
from pipeline.shared.core import DomainError


def trajectory(fps=30):
    """Open, close, hold, release, open, empty close, idle."""
    scale = fps / 30
    return np.concatenate(
        [
            np.full(round(20 * scale), -1.4),
            np.linspace(-1.4, -0.7, round(12 * scale)),
            np.full(round(45 * scale), -0.7),
            np.linspace(-0.7, -1.4, round(12 * scale)),
            np.full(round(20 * scale), -1.4),
            np.linspace(-1.4, -0.05, round(12 * scale)),
            np.full(round(20 * scale), -0.05),
        ]
    ).astype(np.float32)


def arrays():
    x = trajectory()
    rng = np.random.default_rng(123)
    state = rng.normal(size=(len(x), 14)).astype(np.float32)
    state[:, 6] = x
    state[:, 13] = x
    return state, state.copy()


def source_episode(path):
    state, action = arrays()
    write_episode(path, frames=len(state))
    with h5py.File(path, "r+") as handle:
        handle["observations/qpos"][:] = state
        handle["action"][:] = action
    return state, action


@pytest.mark.parametrize("fps", [15, 30, 60])
def test_detects_holding_platform_without_relabeling_open_release_or_idle(fps):
    x = trajectory(fps)
    mask = holding_mask(x, fps, 0, DEFAULT_PARAMETERS)
    assert mask.any()
    # A confirmed platform enters HOLDING at the upward closing edge. The
    # complete phase is selected so action cannot fall back to its raw value
    # between the ramp and the platform.
    assert mask[round(25 * fps / 30) : round(40 * fps / 30)].all()
    assert np.all(x[mask][: round(12 * fps / 30)] <= np.float32(-0.7))
    assert not mask[: round(20 * fps / 30)].any()
    assert not mask[round(80 * fps / 30) :].any()
    assert mask[round(40 * fps / 30) : round(65 * fps / 30)].all()


def test_holding_state_survives_small_downward_jitter_until_confirmed_release():
    fps = 30
    x = np.concatenate(
        [
            np.full(20, -1.4),
            np.linspace(-1.4, -0.7, 12),
            np.full(20, -0.7),
            np.linspace(-0.7, -0.74, 3),
            np.full(20, -0.7),
            np.linspace(-0.7, -1.4, 12),
            np.full(20, -1.4),
        ]
    ).astype(np.float32)
    mask = holding_mask(x, fps, 0, DEFAULT_PARAMETERS)
    closing = round(32 * fps / 30)
    release = round(75 * fps / 30)
    assert mask[closing:release].all()
    assert not mask[release + round(10 * fps / 30) :].any()


def test_shallow_grasp_at_empty_close_level_holds_until_real_release():
    """A shallow grasp may sit on the nominal empty-close level.

    The measured plateau drifts from -0.21 to -0.19, crossing
    ``closed - empty_closed_margin`` (-0.2).  Reaching that level must not end
    the hold: the filtered slope there is ~0 rad/s, so the gripper is still
    holding, and only the later downward movement is a real release.
    """
    fps = 30
    x = np.concatenate(
        [
            np.full(20, -1.4),
            np.linspace(-1.4, -0.21, 12),
            np.full(40, -0.21),
            np.linspace(-0.21, -0.19, 20),
            np.full(40, -0.19),
            np.linspace(-0.19, -1.4, 12),
            np.full(20, -1.4),
        ]
    ).astype(np.float32)
    release = 20 + 12 + 40 + 20 + 40
    mask = holding_mask(x, fps, 0, DEFAULT_PARAMETERS)
    # The whole shallow plateau stays corrected, including the part above the
    # nominal empty level.
    assert mask[25:release].all()
    # The confirmed release still ends the hold, so the tail is untouched.
    assert not mask[release + round(0.5 * fps) :].any()


def test_release_is_detected_from_the_rate_not_from_an_empty_close_level():
    """A slow opening is confirmed from its average rate.

    0.2 rad/s is 6.4% of the nominal travel per second, so a 0.2 s window moves
    0.04.  That clears the travel-scaled ``release_drop`` (0.022 here) long
    before the gripper approaches the fully open position, so the hold ends
    inside the ramp instead of running on to the episode end.  A holding
    plateau drifts two orders of magnitude slower and is never ended this way.
    """

    fps = 30
    x = np.concatenate(
        [
            np.full(20, -1.4),
            np.linspace(-1.4, -0.7, 12),
            np.full(45, -0.7),
            np.linspace(-0.7, -1.1, 60),
            np.full(30, -1.1),
        ]
    ).astype(np.float32)
    release = 20 + 12 + 45
    mask, diagnostics = detect(x, fps, 0, DEFAULT_PARAMETERS, opened=-1.4)
    # The measured span is half of the distance to the configured open level,
    # so the release thresholds shrink with it.
    assert 0 < diagnostics["scale"] < 1
    assert mask[32:release].all()
    assert not mask[release + round(1.5 * fps) :].any()


@pytest.mark.parametrize(
    "x",
    [
        np.full(100, -0.7),  # Cropped static initial holding pose is not a new grasp.
        np.r_[np.full(20, -1.4), np.linspace(-1.4, -0.05, 12), np.full(50, -0.05)],
        np.array([-1.4, -0.7]),  # Too short to infer holding.
        np.r_[np.full(20, -1.4), np.linspace(-1.4, -0.7, 12), np.full(5, -0.7)],
    ],
)
def test_ambiguous_or_empty_closing_is_unchanged(x):
    assert not holding_mask(x, 30, 0, DEFAULT_PARAMETERS).any()


def test_two_grasps_remain_separate_and_opening_motion_is_unchanged():
    x = np.tile(trajectory(), 2)
    mask = holding_mask(x, 30, 0, DEFAULT_PARAMETERS)
    assert mask[: len(x) // 2].any() and mask[len(x) // 2 :].any()
    # Each confirmed hold begins at its closing edge and ends at the
    # confirmed release; the opening and idle portions remain untouched.
    assert mask[25:65].all()
    assert not mask[:20].any()
    assert not mask[80 : len(x) // 2].any()
    assert mask[len(x) // 2 + 25 : len(x) // 2 + 65].all()


def test_detection_follows_observation_state_and_rewrites_only_action():
    state, _ = arrays()
    action = np.full_like(state, np.float32(-1.4))
    original_action = action.copy()
    revised, report = correct_actions(state, action, profile(), {"increase_percent": 10})
    channel = report["channels"]["left_gripper"]
    # The command channel stays open the whole time, so the grasp can only be
    # evidenced by measured feedback.
    assert not detect(action[:, 6], 30, 0, DEFAULT_PARAMETERS, opened=-3.14)[0].any()
    assert channel["evidence"] == "observation.state"
    assert report["evidence_channel"] == "observation.state"
    assert report["algorithm"] == "closing_hold_plateaus_v4"
    assert report["correction"] == "additive_shift_v1"
    assert channel["increase_percent"] == 10
    assert channel["selected_frames"] > 0
    assert channel["changed_frames"] == channel["selected_frames"]
    assert np.array_equal(action, original_action)
    # Interior frames are shifted by exactly 10% of the nominal travel; only the
    # short entry/exit ramp sits between the original action and the shift.
    shift = np.float32(0.10 * 3.14)
    ramp = max(1, round(DEFAULT_PARAMETERS["correction_ramp_seconds"] * 30))
    interval = channel["selected_intervals"][0]
    interior = np.arange(interval["from_frame"] + ramp, interval["to_frame_exclusive"] - ramp)
    assert len(interior) > 0
    assert np.allclose(
        revised[interior, 6] - original_action[interior, 6], shift, atol=1e-6
    )
    assert channel["ramped_frames"] > 0
    for dimension in [*range(6), *range(7, 13)]:
        assert np.array_equal(revised[:, dimension], original_action[:, dimension])


def test_small_range_grasp_scales_minimum_closure_with_observed_travel():
    x = np.concatenate(
        [
            np.full(20, -1.4),
            np.linspace(-1.4, -1.36, 12),
            np.full(30, -1.36),
            np.linspace(-1.36, -1.4, 12),
            np.full(20, -1.4),
        ]
    ).astype(np.float32)
    # 0.04 of travel is below the configured 0.06 admission threshold.
    assert not holding_mask(x, 30, 0, DEFAULT_PARAMETERS).any()
    mask, diagnostics = detect(x, 30, 0, DEFAULT_PARAMETERS, opened=-3.14)
    assert mask.any()
    assert diagnostics["scale"] == DEFAULT_PARAMETERS["adaptive_scale_floor"]
    assert (
        diagnostics["effective_parameters"]["minimum_closure"]
        < DEFAULT_PARAMETERS["minimum_closure"]
    )


def test_noisy_episode_widens_release_confirmation_without_fragmenting_hold():
    fps = 30
    rng = np.random.default_rng(5)
    x = trajectory(fps) + rng.normal(0, 0.02, len(trajectory(fps)))
    mask, diagnostics = detect(x, fps, 0, DEFAULT_PARAMETERS, opened=-3.14)
    effective = diagnostics["effective_parameters"]
    assert diagnostics["noise_band"] > 0
    assert (
        effective["release_confirm_seconds"]
        > DEFAULT_PARAMETERS["release_confirm_seconds"]
    )
    assert (
        effective["minimum_closure"]
        > DEFAULT_PARAMETERS["minimum_closure"] * diagnostics["scale"]
    )
    # One contiguous phase; jitter never splits HOLDING or re-enters after the
    # confirmed release.
    assert np.count_nonzero(np.diff(mask.astype(np.int8)) == 1) == 1
    assert mask[30:60].all()
    assert not mask[:20].any()
    assert not mask[95:].any()


def test_short_retracement_does_not_end_holding():
    x = np.concatenate(
        [
            np.full(20, -1.4),
            np.linspace(-1.4, -0.7, 12),
            np.full(15, -0.7),
            np.array([-0.78, -0.80, -0.72, -0.70], dtype=np.float32),
            np.full(15, -0.7),
            np.linspace(-0.7, -1.4, 12),
            np.full(20, -1.4),
        ]
    ).astype(np.float32)
    mask = holding_mask(x, 30, 0, DEFAULT_PARAMETERS)
    # The dip recovers inside the release window, so HOLDING continues.
    assert mask[32:66].all()
    assert not mask[80:].any()


def test_short_detection_holes_are_filled_before_action_is_written():
    merged, gaps, dropped = _assemble_intervals(
        [(10, 30), (33, 60)], gap_n=5, minimum_n=10
    )
    assert merged == [(10, 60)]
    assert (gaps, dropped) == (1, 0)
    # A short isolated phase is discarded instead of becoming a one-frame spike.
    merged, gaps, dropped = _assemble_intervals(
        [(10, 30), (100, 103)], gap_n=5, minimum_n=10
    )
    assert merged == [(10, 30)]
    assert (gaps, dropped) == (0, 1)


def test_noisy_grasp_produces_one_contiguous_action_interval():
    state, _ = arrays()
    rng = np.random.default_rng(5)
    state[:, 6] += rng.normal(0, 0.01, len(state)).astype(np.float32)
    state[:, 13] += rng.normal(0, 0.01, len(state)).astype(np.float32)
    state[[90, 100], 6] += np.float32(0.08)
    action = np.full_like(state, np.float32(-1.4))
    revised, report = correct_actions(state, action, profile(), {"increase_percent": 10})
    for name in ("left_gripper", "right_gripper"):
        channel = report["channels"][name]
        # A fragmented mask would show up as several alternating intervals.
        assert len(channel["selected_intervals"]) == 1
        assert len(channel["changed_intervals"]) == 1
        assert channel["adaptive"]["filled_gap_count"] >= 0
        dimension = channel["dimension"]
        values = revised[:, dimension]
        # The corrected signal rises once, holds, then falls once.
        steps = np.diff(values)
        rises = np.flatnonzero(steps > 0)
        falls = np.flatnonzero(steps < 0)
        assert rises.max() < falls.min()


def test_action_correction_uses_ramp_and_stays_contiguous():
    state, _ = arrays()
    action = np.full_like(state, np.float32(-1.4))
    revised, report = correct_actions(state, action, profile(), {"increase_percent": 10})
    channel = report["channels"]["left_gripper"]
    assert len(channel["selected_intervals"]) == 1
    ramp = max(1, round(DEFAULT_PARAMETERS["correction_ramp_seconds"] * 30))
    assert channel["ramped_frames"] == 2 * (ramp - 1)
    values = revised[:, 6]
    steps = np.abs(np.diff(values))
    # The original-to-shifted gap is 0.314; a single-frame switch would jump by
    # that much.  The ramp bounds every step to a fraction of it.
    gap = np.float32(0.10 * 3.14)
    assert 0 < steps.max() < gap
    assert steps.max() < gap / 2


def test_action_state_offset_shifts_correction_for_next_frame_labels():
    state, _ = arrays()
    action = np.full_like(state, np.float32(-1.4))
    options = {"increase_percent": 10}
    _, same = correct_actions(state, action, profile(), options)
    _, shifted = correct_actions(
        state, action, profile(), options, action_state_offset=1
    )
    assert same["action_state_offset"] == 0
    assert shifted["action_state_offset"] == 1
    before = same["channels"]["left_gripper"]["selected_intervals"][0]
    after = shifted["channels"]["left_gripper"]["selected_intervals"][0]
    assert after["from_frame"] == before["from_frame"] - 1
    assert after["to_frame_exclusive"] == before["to_frame_exclusive"] - 1


def test_channels_in_one_group_share_one_phase_interval():
    p = profile()
    for channel in p["conversion"]["gripper_closure"]["channels"]:
        channel["group"] = "both"
    state, action = arrays()
    state[:, 13] = state[:, 13] + np.float32(0.02)
    _, report = correct_actions(state, action, p, {"increase_percent": 10})
    left = report["channels"]["left_gripper"]
    right = report["channels"]["right_gripper"]
    assert left["group"] == right["group"] == "both"
    assert left["selected_intervals"] == right["selected_intervals"]
    assert left["adaptive"]["fused_channels"] == ["left_gripper", "right_gripper"]
    # A shared phase also shares one normalized increment so the two channels
    # keep their relative positions.
    assert [
        item["applied_increase_percent"] for item in left["applied_intervals"]
    ] == [item["applied_increase_percent"] for item in right["applied_intervals"]]


def test_additive_shift_keeps_the_original_curve_inside_the_hold():
    state, _ = arrays()
    action = np.full_like(state, np.float32(-1.4))
    # A slow drift inside the sustained grasp: the corrected action must be
    # the same drift shifted up, not a flat level.
    drift = np.linspace(-1.0, -0.8, 36, dtype=np.float32)
    action[35:71, 6] = drift
    action[35:71, 13] = drift
    revised, report = correct_actions(state, action, profile(), {"increase_percent": 10})
    channel = report["channels"]["left_gripper"]
    ramp = max(1, round(DEFAULT_PARAMETERS["correction_ramp_seconds"] * 30))
    interval = channel["selected_intervals"][0]
    interior = np.arange(
        interval["from_frame"] + ramp, interval["to_frame_exclusive"] - ramp
    )
    original = action[interior, 6]
    corrected = revised[interior, 6]
    delta = corrected - original
    # The offset is constant and the shape (frame-to-frame differences) is
    # preserved exactly.
    assert np.ptp(delta) < 1e-6
    assert np.allclose(delta, np.float32(0.10 * 3.14), atol=1e-6)
    assert np.allclose(np.diff(corrected), np.diff(original), atol=1e-6)
    assert not np.allclose(corrected, corrected[0])


def test_additive_shift_is_limited_by_the_interval_peak_without_flattening():
    state, action = arrays()
    original_state = state.copy()
    # One frame is already very close to the nominal fully closed position.
    action[42:50, 13] = -0.15
    original_action = action.copy()
    revised, report = correct_actions(state, action, profile(), {"increase_percent": 10})
    assert np.array_equal(state, original_state)
    assert np.array_equal(action, original_action)
    assert np.array_equal(
        revised[:, [*range(6), *range(7, 13)]], action[:, [*range(6), *range(7, 13)]]
    )
    assert np.all(revised[:, [6, 13]] >= action[:, [6, 13]])
    left = report["channels"]["left_gripper"]
    right = report["channels"]["right_gripper"]
    # 10% of the 3.14 travel is available for the left channel.
    left_shift = left["applied_intervals"][0]["applied_increase_percent"] / 100 * 3.14
    assert abs(left_shift - 0.314) < 1e-9
    assert left["applied_intervals"][0]["limited_by_travel"] is False
    # The right interval peaks at closure(-0.15) = 0.952, so the whole interval
    # gets the remaining 0.0478 of travel instead of a per-frame clamp.
    right_shift = (
        right["applied_intervals"][0]["applied_increase_percent"] / 100 * 3.14
    )
    assert abs(right_shift - 0.15) < 1e-6
    assert right["applied_intervals"][0]["limited_by_travel"] is True
    assert any("接近名义全闭" in warning for warning in report["warnings"])
    ramp = max(1, round(DEFAULT_PARAMETERS["correction_ramp_seconds"] * 30))
    interval = right["selected_intervals"][0]
    interior = np.arange(
        interval["from_frame"] + ramp, interval["to_frame_exclusive"] - ramp
    )
    assert np.allclose(
        revised[interior, 13] - action[interior, 13], right_shift, atol=1e-6
    )
    # The near-full frames land on the fully closed endpoint, not a clamped
    # plateau at an arbitrary value.
    assert np.allclose(revised[42:50, 13], 0.0, atol=1e-6)
    assert report["changed_frames_any_channel"] > 0
    unchanged, no_op = correct_actions(state, action, profile(), {"increase_percent": 0})
    assert np.array_equal(unchanged, action)
    assert no_op["changed_frames_any_channel"] == 0 and no_op["warnings"]


def test_generic_channel_mapping_supports_other_dimensions_and_reversed_direction():
    p = profile()
    p["robot_type"] = "test_six_axis"
    p["joint_names"] = ["j0", "finger", "j2", "j3", "j4", "j5"]
    p["joint_units"] = ["raw"] * 6
    p["conversion"]["gripper_closure"]["channels"] = [
        {"joint": "finger", "open": 3.14, "closed": 0}
    ]
    state = np.zeros((len(trajectory()), 6), dtype=np.float32)
    state[:, 1] = -trajectory()
    action = state.copy()
    action[45:50, 1] = 0.1
    revised, report = correct_actions(state, action, p, {"increase_percent": 10})
    # Closing is the decreasing direction here: the shift moves action towards
    # the closed endpoint instead of increasing the raw number.
    assert np.all(revised[:, 1] <= action[:, 1])
    assert np.allclose(revised[40, 1], 0.6, atol=1e-6)
    assert np.allclose(revised[45:50, 1], 0.0, atol=1e-6)
    assert report["channels"]["finger"]["dimension"] == 1
    assert report["channels"]["finger"]["applied_intervals"][0][
        "limited_by_travel"
    ] is True


def test_interval_already_at_nominal_full_close_is_reported_and_untouched():
    state, _ = arrays()
    action = np.full_like(state, np.float32(-1.4))
    action[:, [6, 13]] = np.float32(0.0)  # already at the nominal closed stop
    revised, report = correct_actions(state, action, profile(), {"increase_percent": 10})
    assert np.array_equal(revised, action)
    assert report["changed_frames_any_channel"] == 0
    for channel in report["channels"].values():
        assert channel["applied_intervals"]
        assert all(
            item["limited_by_travel"] for item in channel["applied_intervals"]
        )
        assert all(
            item["applied_increase_percent"] == 0
            for item in channel["applied_intervals"]
        )
    assert any("名义全闭" in warning for warning in report["warnings"])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(algorithm="unknown"),
        lambda c: c["channels"][0].update(joint="missing_joint"),
        lambda c: c["channels"].append(copy.deepcopy(c["channels"][0])),
        lambda c: c["channels"][0].update(open=0, closed=0),
        lambda c: c["channels"][0].update(open=float("nan")),
        lambda c: c["channels"][0].update(group=1),
        lambda c: c.update(phase_parameters={"median_frames": 4}),
        lambda c: c.update(phase_parameters={"typo": 1}),
        lambda c: c.update(default_increase_percent=101),
        lambda c: c.update(default_increase_percent=-1),
    ],
)
def test_invalid_trusted_configuration_fails_closed(mutate):
    p = profile()
    mutate(p["conversion"]["gripper_closure"])
    with pytest.raises(DomainError):
        capabilities(p)


def test_provider_preserves_source_and_feedback_and_reports_actual_changes(tmp_path):
    source = tmp_path / "episode.hdf5"
    state, action = source_episode(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    provider = AlohaHDF5Provider()
    baseline = provider.prepare(source, tmp_path / "baseline", profile())
    adjusted = provider.prepare(
        source,
        tmp_path / "adjusted",
        profile(),
        options={"gripper_closure": {"increase_percent": 10}},
    )
    with (
        h5py.File(baseline["aligned_path"], "r") as old,
        h5py.File(adjusted["aligned_path"], "r") as new,
    ):
        assert np.array_equal(old["action"][:], action)
        assert np.array_equal(new["observation.state"][:], state)
        assert not np.array_equal(new["action"][:], action)
        for key in old:
            if key.startswith("observation.images."):
                assert all(np.array_equal(a, b) for a, b in zip(old[key], new[key]))
    assert "action_corrections" not in baseline
    assert (
        adjusted["action_corrections"]["gripper_closure"]["changed_frames_any_channel"]
        > 0
    )
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    p = profile()
    p.pop("conversion")
    with pytest.raises(DomainError, match="不支持"):
        provider.prepare(
            source,
            tmp_path / "unsupported",
            p,
            options={"gripper_closure": {"increase_percent": 10}},
        )


def test_api_capability_validation_dedup_snapshot_and_lineage(
    settings_factory, tmp_path
):
    settings = hdf5_settings(settings_factory, tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    source_episode(source / "episode_0.hdf5")
    source_episode(source / "episode_1.hdf5")
    app = create_app(settings, background=False)
    admin = {"Authorization": "Bearer " + settings.token}
    with TestClient(app) as http:
        runtime = http.get("/api/config", headers=admin).json()["runtimes"][0]
        assert (
            runtime["conversion_capabilities"]["gripper_closure"][
                "default_increase_percent"
            ]
            == 10
        )
        assert "channels" not in runtime["conversion_capabilities"]["gripper_closure"]
        imported = http.post(
            "/api/imports",
            headers=admin,
            json={"path": str(source), "runtime_id": "arx"},
        ).json()
        path = f"/api/collections/{imported['collection_id']}/process"
        body = {"instruction": "Unscrew the screw.", "run_doctor": False}
        baseline = http.post(path, headers=admin, json=body).json()
        assert baseline["conversion_options"] == {}
        assert (
            http.post(
                path,
                headers=admin,
                json={**body, "conversion_options": {"gripper_closure": None}},
            ).json()["id"]
            == baseline["id"]
        )
        jobs = []
        for percent in (10, 15):
            response = http.post(
                path,
                headers=admin,
                json={
                    **body,
                    "conversion_options": {
                        "gripper_closure": {"increase_percent": percent}
                    },
                },
            )
            assert response.status_code == 202, response.text
            jobs.append(response.json())
            again = http.post(
                path,
                headers=admin,
                json={
                    **body,
                    "conversion_options": {
                        "gripper_closure": {"increase_percent": float(percent)}
                    },
                },
            )
            assert again.json()["id"] == jobs[-1]["id"]
        assert len({baseline["id"], jobs[0]["id"], jobs[1]["id"]}) == 3
        for value in (-1, 101, True, "90", None):
            response = http.post(
                path,
                headers=admin,
                json={
                    **body,
                    "conversion_options": {
                        "gripper_closure": {"increase_percent": value}
                    },
                },
            )
            assert response.status_code == 422, response.text
        assert (
            http.post(
                path,
                headers=admin,
                json={
                    **body,
                    "conversion_options": {
                        "gripper_closure": {"increase_percent": 10, "dimensions": [6]}
                    },
                },
            ).status_code
            == 422
        )
        assert (
            http.post(
                path,
                headers=admin,
                json={**body, "conversion_options": {"arbitrary_option": {}}},
            ).status_code
            == 422
        )
    # Execute the actual queued snapshots, including the disabled baseline.
    # Closing the HTTP lifespan intentionally stops its worker; use a fresh one.
    # A later deployment edit must not change targets of already queued jobs.
    settings.profiles["arx"]["profile"]["conversion"]["gripper_closure"]["channels"][0][
        "open"
    ] = -6.28
    runner = JobRunner(settings, app.state.ingest)
    for _ in range(3):
        runner.tick()
    result = app.state.ingest.store.get("job", jobs[0]["id"])
    assert result["state"] == "DEMO_READY", result
    assert result["conversion_options"]["gripper_closure"]["increase_percent"] == 10
    assert len(result["alignment_report"]["episodes"]) == 2
    lineage = json.loads((Path(result["work_path"]) / "lineage.json").read_text())
    assert lineage["conversion_options"] == result["conversion_options"]
    assert len(lineage["action_corrections"]) == 2
    for report in lineage["action_corrections"]:
        correction = report["gripper_closure"]
        assert correction["increase_percent"] == 10
        assert correction["algorithm"] == "closing_hold_plateaus_v4"
        assert correction["correction"] == "additive_shift_v1"
        assert correction["changed_frames_any_channel"] > 0
        assert correction["channels"]["left_gripper"]["applied_intervals"][0][
            "applied_increase_percent"
        ] == pytest.approx(10)
    # Work products retain the established shared read/write permission policy.
    assert (
        Path(result["work_path"]) / "aligned/episode0.hdf5"
    ).stat().st_mode & 0o777 == 0o666


def test_unsupported_runtime_rejects_options_at_api_and_runner(settings_factory):
    settings = settings_factory("server")
    app = create_app(settings, background=False)
    uid = seed_upload(app.state.ingest, settings)
    options = {"gripper_closure": {"increase_percent": 10}}
    with TestClient(app) as http:
        admin = {"Authorization": "Bearer " + settings.token}
        assert (
            http.get("/api/config", headers=admin).json()["runtimes"][0][
                "conversion_capabilities"
            ]
            == {}
        )
        response = http.post(
            f"/api/uploads/{uid}/process",
            headers=admin,
            json={"conversion_options": options},
        )
        assert response.status_code == 400 and "不支持" in response.text
    with pytest.raises(DomainError, match="不支持"):
        app.state.runner.enqueue(uid, conversion_options=options)
    assert app.state.ingest.store.all("job") == []


@pytest.mark.parametrize(
    "percent", [float("nan"), float("inf"), -float("inf"), True, "90"]
)
def test_runner_rejects_nonfinite_or_coerced_percentages(percent):
    with pytest.raises(DomainError):
        normalize_options(
            {"gripper_closure": {"increase_percent": percent}}, capabilities(profile())
        )


@pytest.mark.parametrize("target_format", ["v2.1", "v3.0"])
def test_real_conversion_contains_corrected_actions_and_fresh_statistics(
    settings_factory, tmp_path, letools_binary, target_format
):
    settings = hdf5_settings(settings_factory, tmp_path)
    settings.processing_mode = "real"
    settings.letools_bin = letools_binary
    source = tmp_path / "source"
    source.mkdir()
    state, action = source_episode(source / "episode_0.hdf5")
    app = create_app(settings, background=False)
    imported = app.state.imports.import_path(str(source), runtime_id="arx")
    runner = app.state.runner
    job = runner.enqueue_collection(
        imported["collection_id"],
        "Unscrew the screw.",
        target_format,
        False,
        conversion_options={"gripper_closure": {"increase_percent": 10}},
    )
    runner.tick()
    result = app.state.ingest.store.get("job", job["id"])
    assert result["state"] == "CONVERTED", result
    dataset = Path(result["dataset_path"])
    # Inspect through the isolated LeTools interpreter (Arrow is not an API dependency).
    script = """
import json,sys
from pathlib import Path
import numpy as np, pyarrow.parquet as pq
root=Path(sys.argv[1])
tables=[pq.read_table(p) for p in sorted((root/'data').rglob('*.parquet'))]
action=np.concatenate([np.asarray(t['action'].to_pylist()) for t in tables])
state=np.concatenate([np.asarray(t['observation.state'].to_pylist()) for t in tables])
stats_path=root/'meta/stats.json'
stats=json.loads(stats_path.read_text()) if stats_path.exists() else json.loads((root/'meta/episodes_stats.jsonl').read_text().splitlines()[0])['stats']
print(json.dumps({'action':action.tolist(),'state':state.tolist(),'stats':stats['action']}))
"""
    python = str(Path(letools_binary).parent / "python")
    payload = json.loads(
        subprocess.check_output([python, "-c", script, str(dataset)], text=True)
    )
    revised = np.asarray(payload["action"], dtype=np.float32)
    assert np.array_equal(payload["state"], state)
    assert np.all(revised[:, [6, 13]] >= action[:, [6, 13]])
    # action[t] == state[t] here, so the sustained grasp sits at -0.7 and the
    # 10% additive shift moves it to -0.386 without flattening anything.
    assert np.isclose(revised[45, 6], -0.386, atol=1e-6)
    assert np.array_equal(revised[:, 0], action[:, 0])
    assert np.allclose(payload["stats"]["mean"], revised.mean(axis=0), atol=1e-6)
    assert np.allclose(payload["stats"]["min"], revised.min(axis=0))
    assert dataset.stat().st_mode & 0o777 == 0o777
