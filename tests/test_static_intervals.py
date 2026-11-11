"""Unit, API and real-workdir tests for whole-recording static trimming."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.server.app import create_app
from pipeline.server.providers.base import configured_interval_policy
from pipeline.server.providers.tianji import TianjiProvider
from pipeline.server.static_intervals import (
    ALGORITHM,
    detect_dataset,
    detect_episode,
    normalize_policy,
    resolve_policy,
    summarize,
)
from pipeline.shared.core import DomainError

SCENARIO = Path(__file__).parent / "fixtures/static_scenario.py"
FPS = 10

FEATURES = {
    "observation.state": {
        "dtype": "float32",
        "shape": [2],
        "names": ["joint0", "joint1"],
        "units": ["rad", "rad"],
    }
}


def admin(settings):
    return {"Authorization": "Bearer " + settings.token}


def policy(**overrides):
    base = {
        "feature": "observation.state",
        "min_motion_seconds": 0.2,
        "jitter_amplitude_seconds": 0.2,
        "min_output_seconds": 0.0,
    }
    base.update(overrides)
    return resolve_policy(normalize_policy(base), FEATURES, FPS)


def moving(state, count, delta=0.5):
    return [
        [state[0] + delta * (index + 1), state[1] + delta * (index + 1)]
        for index in range(count)
    ]


# --------------------------------------------------------------------------- unit
def test_normalize_policy_defaults_and_rejects_invalid_values():
    value = normalize_policy({})
    assert value["feature"] == "observation.state"
    assert value["min_motion_seconds"] == 0.2
    assert value["min_output_seconds"] == 1.0
    assert "min_static_seconds" not in value
    assert "padding_seconds" not in value
    with pytest.raises(DomainError, match="未知字段"):
        normalize_policy({"min_static_second": 1})
    with pytest.raises(DomainError, match="必须在"):
        normalize_policy({"min_motion_seconds": -1})
    with pytest.raises(DomainError, match="未知字段"):
        normalize_policy({"min_static_seconds": 1})
    with pytest.raises(DomainError, match="未知字段"):
        normalize_policy({"padding_seconds": 0.2})
    with pytest.raises(DomainError, match="正数"):
        normalize_policy({"default_threshold": 0})


def test_provider_interval_policy_inherits_the_trim_signal():
    policy = configured_interval_policy(
        {
            "trim": {
                "feature": "action",
                "unit_thresholds": {"rad": 0.05},
                "min_idle_seconds": 9,
            },
            "static_intervals": {
                "min_motion_seconds": 0.3,
                "min_output_seconds": 2,
                "unknown": 1,
            },
        }
    )
    assert policy["feature"] == "action"
    assert policy["unit_thresholds"] == {"rad": 0.05}
    assert "min_idle_seconds" not in policy
    assert policy["min_motion_seconds"] == 0.3
    assert policy["min_output_seconds"] == 2
    assert "min_static_seconds" not in policy
    assert "padding_seconds" not in policy
    assert "unknown" not in policy
    assert configured_interval_policy({"trim": "invalid"}) == {}
    tianji = TianjiProvider().stationary_interval_policy(
        {
            "tianji": {
                "motion_joint_threshold_rad": 0.01,
                "motion_gripper_threshold": 0.2,
            }
        }
    )
    assert tianji["threshold_per_frame"] is True
    assert tianji["unit_thresholds"] == {"rad": 0.01, "normalized": 0.2}


def test_rad_units_need_unit_thresholds_not_default_threshold():
    # A built-in per-unit threshold (rad -> 0.02) wins over default_threshold,
    # so a Profile that only raises default_threshold keeps the old sensitivity
    # on rad joints. The unit itself has to be overridden.
    ignored = resolve_policy(normalize_policy({"default_threshold": 0.1}), FEATURES, FPS)
    assert ignored["thresholds"] == [0.02, 0.02]
    explicit = resolve_policy(
        normalize_policy(
            {"default_threshold": 0.1, "unit_thresholds": {"rad": 0.1}}
        ),
        FEATURES,
        FPS,
    )
    assert explicit["thresholds"] == [0.1, 0.1]


def test_arx_profile_overrides_the_rad_threshold():
    profile = json.loads(Path("configs/arx-x5-direct-teaching-v1.json").read_text())
    resolved = resolve_policy(
        normalize_policy(configured_interval_policy(profile)), FEATURES, FPS
    )
    assert resolved["thresholds"] == [0.1, 0.1]
    assert "min_static_seconds" not in resolved
    assert "padding_seconds" not in resolved


def test_detect_episode_removes_head_middle_and_tail_static_spans():
    values = []
    values += [[0.0, 0.0]] * 12
    values += moving([0.0, 0.0], 10)
    values += [[5.0, 5.0]] * 10
    values += moving([5.0, 5.0], 10)
    values += [[10.0, 10.0]] * 10
    result = detect_episode(values, policy())
    assert result["status"] == "trimmed"
    assert result["keep_ranges"] == [[12, 22], [32, 42]]
    assert result["removed_ranges"] == [[0, 12], [22, 32], [42, 52]]
    assert result["keep_frames"] == 20
    assert result["removed_frames"] == 32
    assert result["output_episodes"] == 1


def test_single_static_frames_are_removed_without_a_minimum_duration():
    # Two moving runs are separated by one unchanged frame. The first frame is
    # also static because there is no predecessor. Both must be removed even
    # though the static spans are only one frame long.
    values = moving([0.0, 0.0], 12)
    values += [[6.0, 6.0]]
    values += moving([6.0, 6.0], 12)
    result = detect_episode(values, policy())
    assert result["static_ranges"] == [[0, 1], [12, 13]]
    assert result["removed_ranges"] == [[0, 1], [12, 13]]
    assert result["keep_ranges"] == [[1, 12], [13, 25]]
    assert result["removed_frames"] == 2
    assert result["output_episodes"] == 1


def test_static_spans_are_removed_without_boundary_context():
    values = []
    values += [[0.0, 0.0]] * 12
    values += moving([0.0, 0.0], 10)
    values += [[5.0, 5.0]] * 10
    values += moving([5.0, 5.0], 10)
    values += [[10.0, 10.0]] * 10
    result = detect_episode(values, policy())
    assert result["removed_ranges"] == [[0, 12], [22, 32], [42, 52]]
    assert result["removed_frames"] == 32


def test_detect_episode_ignores_small_jitter_but_splits_on_real_motion():
    values = [[0.0, 0.0]] * 15
    values += [[0.003, 0.0]]
    values += [[0.003, 0.0]] * 14
    values += [[0.503, 0.0], [1.003, 0.0]]
    values += [[1.003, 0.0]] * 15
    result = detect_episode(values, policy())
    assert result["removed_ranges"] == [[0, 30], [32, 47]]
    assert result["keep_ranges"] == [[30, 32]]
    assert result["status"] == "trimmed"


def test_detect_episode_keeps_and_marks_a_fully_static_episode():
    values = [[0.25, 0.25]] * 20
    result = detect_episode(values, policy())
    assert result["status"] == "static"
    assert result["keep_ranges"] == [[0, 20]]
    assert result["removed_ranges"] == []
    assert result["removed_frames"] == 0
    assert "保留" in result["reason"]


def test_short_action_between_static_spans_is_dropped_with_the_static_spans():
    values = [[0.0, 0.0]] * 10
    values += moving([0.0, 0.0], 2)
    values += [[1.0, 1.0]] * 10
    values += moving([1.0, 1.0], 5)
    values += [[3.5, 3.5]] * 10
    result = detect_episode(values, policy(min_output_seconds=0.5))
    assert result["removed_ranges"] == [[0, 22], [27, 37]]
    assert result["keep_ranges"] == [[22, 27]]
    assert result["removed_frames"] == 32
    assert result["status"] == "trimmed"


def test_static_spans_at_start_and_end_are_removed_with_short_motion_stubs():
    # Five motion frames, a long static wait, a real action, another wait and a
    # short trailing motion stub: both waits must be removed even though the
    # stubs left at the recording boundaries are shorter than min_output_seconds.
    values = moving([0.0, 0.0], 5)
    values += [[2.5, 2.5]] * 100
    values += moving([2.5, 2.5], 120)
    values += [[62.5, 62.5]] * 100
    values += moving([62.5, 62.5], 6)
    assert len(values) == 331
    result = detect_episode(values, policy(min_output_seconds=1.0))
    assert result["static_ranges"] == [[0, 1], [5, 105], [225, 325]]
    assert result["removed_ranges"] == [[0, 105], [225, 331]]
    assert result["keep_ranges"] == [[105, 225]]
    assert result["removed_frames"] == 211
    assert result["output_episodes"] == 1
    assert result["status"] == "trimmed"


def test_all_static_is_kept_when_every_motion_span_is_below_min_output():
    values = [[0.0, 0.0]] * 10
    values += [[0.5, 0.5], [1.0, 1.0]]
    values += [[1.0, 1.0]] * 10
    values += [[1.5, 1.5], [2.0, 2.0]]
    values += [[2.0, 2.0]] * 10
    result = detect_episode(values, policy(min_output_seconds=0.5))
    # The last remaining span is never dropped, so the recording cannot vanish
    # while every static range around it is still removed.
    assert result["keep_ranges"] == [[22, 24]]
    assert result["removed_ranges"] == [[0, 22], [24, 34]]
    assert result["removed_frames"] == 32


def test_detection_reports_non_finite_values_and_builds_a_summary():
    values = [[0.0, 0.0]] * 4 + [[float("nan"), 0.0]] * 4
    plan = detect_dataset({0: values}, policy())
    assert plan["algorithm"] == ALGORITHM
    assert plan["episodes"][0]["status"] == "invalid"
    assert plan["episodes"][0]["keep_ranges"] == [[0, 8]]
    summary = summarize(plan["episodes"], FPS)
    assert summary["episodes"] == 1
    assert summary["episodes_removed"] == 0
    assert summary["frames_removed"] == 0


# ------------------------------------------------------------------ conversion API
def test_conversion_static_intervals_is_boolean_real_mode_and_exclusive(
    settings_factory,
):
    settings = settings_factory("server")
    app = create_app(settings, background=False)
    auth = admin(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/uploads/missing/process",
            headers=auth,
            json={"trim_static_intervals": True},
        )
        assert response.status_code == 400
        assert "演示模式" in response.text
        response = client.post(
            "/api/uploads/missing/process",
            headers=auth,
            json={"trim_idle_frames": True, "trim_static_intervals": True},
        )
        assert response.status_code == 400
        assert "不能同时开启" in response.text
        assert (
            client.post(
                "/api/uploads/missing/process",
                headers=auth,
                json={"trim_static_intervals": "yes"},
            ).status_code
            == 422
        )
    with pytest.raises(DomainError, match="布尔值"):
        app.state.runner.enqueue("missing", trim_static_intervals=1)
    with pytest.raises(DomainError, match="不能同时开启"):
        app.state.runner.enqueue(
            "missing", trim_idle_frames=True, trim_static_intervals=True
        )


# ------------------------------------------------------------------- real worker
@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_real_static_interval_trim_joins_kept_ranges_into_one_episode(
    letools_binary, tmp_path, version
):
    python = str(Path(letools_binary).with_name("python"))
    base = tmp_path / "static"
    subprocess.run(
        [python, str(SCENARIO), str(base), version],
        check=True,
        env={**os.environ, "PYTHONPATH": str(Path.cwd())},
    )
    output = base / "output"
    inspected = json.loads(
        subprocess.run(
            [python, str(SCENARIO), "inspect", str(output)],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path.cwd())},
        ).stdout
    )
    assert inspected["valid"] is True
    assert [row["length"] for row in inspected["episodes"]] == [6, 21]
    assert [row["episode_index"][0] for row in inspected["episodes"]] == [0, 1]
    assert sum(inspected["videos"].values()) == 27
    info = json.loads((output / "meta/info.json").read_text())
    assert info["total_episodes"] == 2
    assert info["total_frames"] == 27
