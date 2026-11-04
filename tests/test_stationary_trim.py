"""Unit, API and real-workdir tests for idle boundary trimming."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.client.adapters.mock import MockAdapter
from pipeline.server.app import create_app
from pipeline.server.providers.base import configured_trim_policy
from pipeline.server.providers.tianji import TianjiProvider
from pipeline.server.quality import DOCTOR_REQUIRED_CHECKS
from pipeline.server.stationary_trim import (
    ALGORITHM,
    detect_dataset,
    detect_episode,
    normalize_policy,
    resolve_policy,
    summarize,
)
from pipeline.shared.core import DomainError, digest

SCENARIO = Path(__file__).parent / "fixtures/trim_scenario.py"

FEATURES = {
    "observation.state": {
        "dtype": "float32",
        "shape": [2],
        "names": ["joint0", "joint1"],
        "units": ["rad", "normalized"],
    }
}


def admin(settings):
    return {"Authorization": "Bearer " + settings.token}


def policy(**overrides):
    base = {
        "feature": "observation.state",
        "min_idle_seconds": 0.2,
        "min_active_seconds": 0.1,
        "padding_before_seconds": 0.0,
        "padding_after_seconds": 0.0,
    }
    base.update(overrides)
    return resolve_policy(normalize_policy(base), FEATURES, 10)


def seed(app, jid="source", *, path=None, **updates):
    settings = app.state.runner.settings
    path = Path(path or settings.root / "work" / jid / "dataset")
    if not (path / "meta/info.json").exists():
        (path / "meta").mkdir(parents=True, exist_ok=True)
        info = {
            "codebase_version": "v3.0",
            "robot_type": "test_arm",
            "fps": 10,
            "total_episodes": 2,
            "total_frames": 11,
            "splits": {"train": "0:2"},
            "features": FEATURES,
            **updates,
        }
        (path / "meta/info.json").write_text(json.dumps(info))
    info = json.loads((path / "meta/info.json").read_text())
    job = {
        "id": jid,
        "key": jid,
        "state": "CONVERTED",
        "dataset_name": "source dataset",
        "robot_type": info["robot_type"],
        "created_at": 1,
        "dataset_ready": True,
        "dataset_path": str(path),
        "target_format": info["codebase_version"],
        "episode_count": info["total_episodes"],
        "frame_count": info["total_frames"],
    }
    app.state.ingest.store.put("job", jid, job)
    return job, path


def completed_preview(app, preview_id, episodes=None):
    trims = app.state.trims
    row = app.state.ingest.store.get("trim_preview", preview_id)
    episodes = episodes or [
        {
            "episode_index": index,
            "frames": 6,
            "keep_start": 1,
            "keep_stop": 5,
            "head_removed": 1,
            "tail_removed": 1,
            "active_start": 1,
            "active_stop": 5,
            "status": "trimmed",
            "reason": "",
        }
        for index in range(row["source"]["episode_count"])
    ]
    summary = summarize(episodes, row["source"]["fps"])
    trims._save_preview(
        row,
        state="COMPLETED",
        stage="首尾静止帧检测完成",
        episodes=episodes,
        summary=summary,
        plan_digest=digest({"episodes": episodes}),
    )
    return row


# --------------------------------------------------------------------------- unit
def test_normalize_policy_applies_defaults_and_rejects_unknown_keys():
    value = normalize_policy({})
    assert value["feature"] == "observation.state"
    assert value["threshold_per_frame"] is False
    assert value["min_idle_seconds"] == 0.5
    assert value["remove_fully_static"] is False
    with pytest.raises(DomainError, match="未知字段"):
        normalize_policy({"min_idle_second": 1})
    with pytest.raises(DomainError, match="布尔"):
        normalize_policy({"trim_start": "yes"})
    with pytest.raises(DomainError, match="秒"):
        normalize_policy({"min_idle_seconds": -1})
    with pytest.raises(DomainError, match="正数"):
        normalize_policy({"unit_thresholds": {"rad": 0}})


def test_resolve_policy_uses_declared_units_and_per_frame_thresholds():
    resolved = resolve_policy(normalize_policy({}), FEATURES, 10)
    assert resolved["feature"] == "observation.state"
    assert resolved["dimensions"] == 2
    # Per-second thresholds: rad and normalized are not scaled by FPS.
    assert resolved["thresholds"] == [0.02, 0.02]
    scaled = resolve_policy(
        normalize_policy({"threshold_per_frame": True}), FEATURES, 10
    )
    assert scaled["thresholds"] == [0.2, 0.2]
    custom = resolve_policy(
        normalize_policy({"unit_thresholds": {"rad": 0.5}, "threshold_per_frame": True}),
        FEATURES,
        10,
    )
    assert custom["thresholds"] == [5.0, 0.2]


def test_resolve_policy_falls_back_to_candidates_and_rejects_impossible_specs():
    features = {**FEATURES, "action": FEATURES["observation.state"]}
    resolved = resolve_policy(
        normalize_policy({"feature": "missing.feature"}), features, 10
    )
    assert resolved["feature"] == "observation.state"
    custom = resolve_policy(
        normalize_policy(
            {"feature": "missing.feature", "candidate_features": ["action"]}
        ),
        {"action": FEATURES["observation.state"]},
        10,
    )
    assert custom["feature"] == "action"
    with pytest.raises(DomainError, match="数值特征"):
        resolve_policy(normalize_policy({}), {"observation.state": {"dtype": "video"}}, 10)
    with pytest.raises(DomainError, match="shape"):
        resolve_policy(
            normalize_policy({}), {"observation.state": {"dtype": "float32"}}, 10
        )
    with pytest.raises(DomainError, match="排除"):
        resolve_policy(
            normalize_policy({}),
            {
                "observation.state": {
                    "dtype": "float32",
                    "shape": [1],
                    "units": ["padding"],
                }
            },
            10,
        )
    # A dataset that does not declare units must still be usable: it falls back
    # to the policy's default threshold instead of silently disabling trimming.
    undeclared = resolve_policy(
        normalize_policy({}), {"observation.state": {"dtype": "float32", "shape": [1]}}, 10
    )
    assert undeclared["thresholds"] == [0.02]
    assert resolve_policy(
        normalize_policy({"default_threshold": 0.5}),
        {
            "observation.state": {
                "dtype": "float32",
                "shape": [1],
                "units": ["unknown"],
            }
        },
        10,
    )["thresholds"] == [0.5]
    with pytest.raises(DomainError, match="FPS"):
        resolve_policy(normalize_policy({}), FEATURES, 0)


def test_provider_hook_reads_only_the_trusted_trim_block():
    assert configured_trim_policy(
        {"trim": {"feature": "action", "min_idle_seconds": 1, "other": 2}}
    ) == {"feature": "action", "min_idle_seconds": 1}
    assert configured_trim_policy({"trim": "invalid"}) == {}
    tianji = TianjiProvider().stationary_trim_policy(
        {
            "tianji": {
                "motion_joint_threshold_rad": 0.01,
                "motion_gripper_threshold": 0.2,
            }
        }
    )
    assert tianji["threshold_per_frame"] is True
    assert tianji["feature"] == "observation.state"
    assert tianji["unit_thresholds"] == {"rad": 0.01, "normalized": 0.2}


values = [
    {"frame": 0, "state": [0.0, 0.0]},
    {"frame": 1, "state": [0.0, 0.0]},
    {"frame": 2, "state": [0.1, 0.1]},
    {"frame": 3, "state": [0.2, 0.2]},
    {"frame": 4, "state": [0.3, 0.3]},
    {"frame": 5, "state": [0.3, 0.3]},
    {"frame": 6, "state": [0.3, 0.3]},
]


def test_detect_episode_trims_only_boundary_idle_frames():
    result = detect_episode([row["state"] for row in values], policy())
    assert result["keep_start"] == 2
    assert result["keep_stop"] == 5
    assert result["head_removed"] == 2
    assert result["tail_removed"] == 2
    assert result["status"] == "trimmed"


def test_detect_episode_preserves_mid_episode_pauses_and_short_idle():
    sequence = [0, 1, 1, 1, 2, 2, 2, 2, 2, 2]
    result = detect_episode(
        [[float(value), float(value)] for value in sequence], policy()
    )
    # The pause at frames 2–3 stays and the short head idle is not trimmed.
    assert result["keep_start"] == 0
    assert result["keep_stop"] == 5


def test_padding_does_not_hide_a_long_enough_idle_span():
    sequence = [0.0, 0.0, 0.0, 0.1, 0.2, 0.3, 0.3, 0.3]
    result = detect_episode(
        [[value, value] for value in sequence],
        policy(min_idle_seconds=0.2, padding_before_seconds=0.2),
    )
    assert result["keep_start"] == 1
    assert result["head_removed"] == 1
    # Tail idle is exactly the minimum, so it is still trimmed.
    assert result["keep_stop"] == 6
    assert result["status"] == "trimmed"


def test_detect_episode_static_episode_and_padding_and_validation():
    static = detect_episode([[0.0, 0.0]] * 6, policy())
    assert static["status"] == "static"
    assert static["keep_start"] == 0 and static["keep_stop"] == 6
    removed = detect_episode(
        [[0.0, 0.0]] * 6, policy(remove_fully_static=True)
    )
    assert removed["status"] == "removed"
    assert removed["keep_stop"] - removed["keep_start"] == 0
    padded = detect_episode(
        [
            [0.0, 0.0],
            [0.0, 0.0],
            [0.0, 0.0],
            [0.1, 0.1],
            [0.2, 0.2],
            [0.3, 0.3],
            [0.3, 0.3],
            [0.3, 0.3],
        ],
        policy(padding_before_seconds=0.2, min_idle_seconds=0.0),
    )
    assert padded["keep_start"] == 1
    with pytest.raises(ValueError, match="非有限"):
        detect_episode([[0.0, 0.0], [float("nan"), 0.0]], policy())
    short = detect_episode([[0.0, 0.0]], policy())
    assert short["status"] == "too_short"


def test_detect_dataset_adds_episode_index_and_summary():
    plan = detect_dataset(
        {0: [row["state"] for row in values], 1: [[0.0, 0.0]] * 6},
        policy(),
    )
    assert plan["algorithm"] == ALGORITHM
    assert [row["episode_index"] for row in plan["episodes"]] == [0, 1]
    assert plan["summary"]["episodes"] == 2
    assert plan["summary"]["episodes_trimmed"] == 1
    assert plan["summary"]["episodes_static"] == 1
    assert plan["summary"]["frames_removed"] == 4


# ---------------------------------------------------------------------------- api
def test_trim_api_auth_candidates_policy_and_preview_dedup(settings_factory):
    settings = settings_factory("server", letools_bin=sys.executable)
    app = create_app(settings, background=False)
    seed(app)
    auth = admin(settings)
    with TestClient(app) as client:
        assert client.get("/api/trim-candidates").status_code == 401
        assert (
            client.get(
                "/api/jobs/source/trim-policy",
                headers={"Authorization": "Bearer " + settings.device_token},
            ).status_code
            == 401
        )
        rows = client.get("/api/trim-candidates", headers=auth).json()
        row = next(item for item in rows if item["source_job_id"] == "source")
        assert row["selectable"] is True
        assert row["policy_feature"] == "observation.state"
        assert row["source_format"] == "v3.0"
        assert row["episode_count"] == 2

        result = client.get("/api/jobs/source/trim-policy", headers=auth).json()
        assert result["available"] is True
        assert result["feature"] == "observation.state"
        assert result["units"] == ["rad", "normalized"]
        assert result["defaults"]["min_idle_seconds"] == 0.5
        assert result["defaults"]["threshold_scale"] == 1.0

        assert (
            client.post(
                "/api/jobs/source/trim-previews",
                headers=auth,
                json={"options": {"bogus": 1}},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/jobs/source/trim-previews",
                headers=auth,
                json={"options": {"min_idle_seconds": -1}},
            ).status_code
            == 422
        )
        body = {
            "options": {
                "min_idle_seconds": 0.1,
                "min_active_seconds": 0.1,
                "padding_before_seconds": 0,
                "padding_after_seconds": 0,
            }
        }
        response = client.post(
            "/api/jobs/source/trim-previews", headers=auth, json=body
        )
        assert response.status_code == 202, response.text
        preview = response.json()
        assert preview["state"] == "QUEUED"
        assert preview["source"]["episode_count"] == 2
        assert preview["policy"]["thresholds"] == [0.02, 0.02]
        deduped = client.post(
            "/api/jobs/source/trim-previews", headers=auth, json=body
        ).json()
        assert deduped["id"] == preview["id"]

        changed = client.post(
            "/api/jobs/source/trim-previews",
            headers=auth,
            json={"options": {"threshold_scale": 2}},
        ).json()
        assert changed["id"] != preview["id"]
        assert changed["policy"]["thresholds"] == [0.04, 0.04]

        listing = client.get(
            "/api/jobs/source/trim-previews", headers=auth
        ).json()
        assert {item["id"] for item in listing} >= {preview["id"], changed["id"]}
        assert client.get("/api/jobs/missing/trim-previews", headers=auth).status_code == 404

        completed = completed_preview(app, preview["id"])
        page = client.get(f"/api/trim-previews/{completed['id']}", headers=auth).json()
        assert page["state"] == "COMPLETED"
        assert page["summary"]["frames_removed"] == 4
        assert len(page["episodes"]) == 2
        assert page["total"] == 2
        limited = client.get(
            f"/api/trim-previews/{completed['id']}?offset=1&limit=1", headers=auth
        ).json()
        assert limited["total"] == 2
        assert len(limited["episodes"]) == 1


def test_trim_enqueue_requires_a_completed_matching_preview(settings_factory):
    settings = settings_factory("server", letools_bin=sys.executable)
    app = create_app(settings, background=False)
    seed(app)
    auth = admin(settings)
    with TestClient(app) as client:
        previews = client.get("/api/jobs/source/trim-previews", headers=auth).json()
        assert previews == []
        pending = client.post("/api/jobs/source/trim-previews", headers=auth, json={}).json()
        assert (
            client.post(
                "/api/jobs/source/trims",
                headers=auth,
                json={"preview_id": pending["id"]},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/jobs/source/trims",
                headers=auth,
                json={"preview_id": pending["id"], "run_doctor": "yes"},
            ).status_code
            == 422
        )
        completed = completed_preview(app, pending["id"])
        assert (
            client.post(
                "/api/jobs/source/trims",
                headers=auth,
                json={"preview_id": completed["id"], "run_doctor": False},
            ).status_code
            == 202
        )
        jobs = client.get("/api/jobs", headers=auth).json()
        job = next(row for row in jobs if row.get("operation") == "stationary_trim")
        assert job["operation"] == "stationary_trim"
        assert job["source_job_id"] == "source"
        assert job["dataset_name"] == "source dataset · 首尾裁剪"
        assert job["episode_count"] == 2
        assert job["frame_count"] == 8
        assert job["capture_mode"] == "stationary_trim"
        assert job["dataset_available"] is False

        body = {"preview_id": completed["id"], "run_doctor": True}
        first = client.post("/api/jobs/source/trims", headers=auth, json=body)
        second = client.post("/api/jobs/source/trims", headers=auth, json=body)
        assert first.json()["id"] == second.json()["id"]
        named = client.post(
            "/api/jobs/source/trims",
            headers=auth,
            json={"preview_id": completed["id"], "name": "裁剪副本"},
        )
        assert named.json()["dataset_name"] == "裁剪副本"
        assert named.json()["id"] != first.json()["id"]


def test_trim_enqueue_rejects_foreign_and_stale_previews(settings_factory):
    settings = settings_factory("server", letools_bin=sys.executable)
    app = create_app(settings, background=False)
    _, first_path = seed(app, "source")
    seed(app, "other")
    auth = admin(settings)
    with TestClient(app) as client:
        preview = client.post(
            "/api/jobs/other/trim-previews", headers=auth, json={}
        ).json()
        completed_preview(app, preview["id"])
        assert (
            client.post(
                "/api/jobs/source/trims",
                headers=auth,
                json={"preview_id": preview["id"]},
            ).status_code
            == 409
        )
        own = client.post(
            "/api/jobs/source/trim-previews", headers=auth, json={}
        ).json()
        completed = completed_preview(app, own["id"])
        (first_path / "meta/extra.json").write_text("{}")
        response = client.post(
            "/api/jobs/source/trims",
            headers=auth,
            json={"preview_id": completed["id"]},
        )
        assert response.status_code == 409
        assert "发生变化" in response.json()["detail"]


def test_trim_is_unavailable_without_the_toolchain(settings_factory):
    settings = settings_factory("server", letools_bin="definitely-missing-tool")
    app = create_app(settings, background=False)
    seed(app)
    auth = admin(settings)
    with TestClient(app) as client:
        row = next(
            item
            for item in client.get("/api/trim-candidates", headers=auth).json()
            if item["source_job_id"] == "source"
        )
        assert row["selectable"] is False
        assert "LeTools" in row["reason"]
        result = client.get("/api/jobs/source/trim-policy", headers=auth).json()
        assert result["available"] is False
        assert "LeTools" in result["reason"]
        assert (
            client.post(
                "/api/jobs/source/trim-previews", headers=auth, json={}
            ).status_code
            == 503
        )


# --------------------------------------------------------------- doctor gating
def _doctor_report(total_episodes, total_frames, severity):
    return {
        "total_episodes": total_episodes,
        "total_frames": total_frames,
        "overall_severity": severity,
        "checks": [
            {"name": name, "severity": severity, "messages": []}
            for name in sorted(DOCTOR_REQUIRED_CHECKS)
        ],
    }


def _mock_trim_commands(runner, monkeypatch, doctor_severity):
    opened, closed, calls = [], [], []
    monkeypatch.setattr(
        runner,
        "_open_allocation",
        lambda work: opened.append(work) or "allocation-71",
    )
    monkeypatch.setattr(runner, "_close_allocation", lambda: closed.append(True))

    def command(args, work, name, allocation=None):
        assert allocation == "allocation-71"
        calls.append(name)
        output, error = work / f"{name}.json", work / f"{name}.stderr.log"
        error.write_text("")
        if name == "trim":
            dataset = work / "dataset"
            (dataset / "meta").mkdir(parents=True)
            (dataset / "meta/info.json").write_text(
                json.dumps(
                    {
                        "codebase_version": "v3.0",
                        "total_episodes": 2,
                        "total_frames": 8,
                    }
                )
            )
            output.write_text(
                json.dumps(
                    {
                        "validated": True,
                        "version": "v3.0",
                        "episodes": 2,
                        "frames": 8,
                        "frames_before": 12,
                        "mapping": {"0": 0, "1": 1},
                    }
                )
            )
        elif name == "doctor":
            output.write_text(
                json.dumps(_doctor_report(2, 8, doctor_severity))
            )
        else:
            output.write_text("")
        return (1 if name == "doctor" and doctor_severity != "PASS" else 0), output, error

    monkeypatch.setattr(runner, "command", command)
    return opened, closed, calls


@pytest.mark.parametrize(
    ("severity", "state", "allocation"),
    [
        ("PASS", "COMPLETED", "allocation-71"),
        ("WARN", "QUARANTINED", "allocation-71"),
    ],
)
def test_trim_runs_doctor_in_the_same_allocation_and_gates_the_result(
    settings_factory, monkeypatch, severity, state, allocation
):
    settings = settings_factory("server", letools_bin=sys.executable)
    app = create_app(settings, background=False)
    seed(app)
    runner, trims = app.state.runner, app.state.trims
    opened, closed, calls = _mock_trim_commands(runner, monkeypatch, severity)
    preview = trims.enqueue_preview("source", {"min_idle_seconds": 0.1})
    completed = completed_preview(app, preview["id"])
    job = trims.enqueue("source", completed["id"], run_doctor=True)
    runner.tick()
    result = runner.store.get("job", job["id"])
    assert result["state"] == state, result
    assert result["dataset_ready"] is True
    assert result["doctor_report"]["overall_severity"] == severity
    assert result["trim_report"]["frames"] == 8
    assert calls == ["trim", "validate", "doctor"]
    assert opened and len(closed) == 1


# ------------------------------------------------------------------- real worker
@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_real_preview_then_trim_job_produces_a_new_dataset(
    settings_factory, tmp_path, letools_binary, version
):
    python = str(Path(letools_binary).with_name("python"))
    settings = settings_factory(
        "server",
        letools_bin=letools_binary,
        min_free_bytes=0,
        command_timeout=600,
    )
    source_parent = settings.root / "work" / "source"
    subprocess.run(
        [python, str(SCENARIO), "create", str(source_parent), version],
        check=True,
        env={**os.environ, "PYTHONPATH": str(Path.cwd())},
    )
    source = source_parent / "source"
    source.rename(source_parent / "dataset")

    app = create_app(settings, background=False)
    job, path = seed(app, "source", path=source_parent / "dataset")
    auth = admin(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs/source/trim-previews",
            headers=auth,
            json={
                "options": {
                    "min_idle_seconds": 0,
                    "min_active_seconds": 0.1,
                    "padding_before_seconds": 0,
                    "padding_after_seconds": 0,
                }
            },
        )
        assert response.status_code == 202, response.text
        preview = response.json()
        assert preview["source_snapshot"]
        assert app.state.trims.tick() is True

        completed = client.get(
            f"/api/trim-previews/{preview['id']}", headers=auth
        ).json()
        assert completed["state"] == "COMPLETED", completed
        assert completed["summary"]["frames_removed"] == 9
        assert [row["keep_start"] for row in completed["episodes"]] == [2, 2, 2]
        assert [row["keep_stop"] for row in completed["episodes"]] == [6, 6, 6]
        assert completed["plan_digest"]

        response = client.post(
            "/api/jobs/source/trims",
            headers=auth,
            json={"preview_id": preview["id"], "run_doctor": False},
        )
        assert response.status_code == 202, response.text
        trimmed_job = response.json()
        assert trimmed_job["episode_count"] == 3
        assert trimmed_job["frame_count"] == 12
        assert app.state.runner.tick() is True

        result = client.get(
            f"/api/jobs/{trimmed_job['id']}", headers=auth
        ).json()
        assert result["state"] == "CONVERTED", result
        assert result["dataset_ready"] is True
        assert result["target_format"] == version
        assert result["dataset_path"]
        output = Path(result["dataset_path"])
        assert output.is_relative_to(settings.root)
        assert (output / "meta/bodypipeline-stationary-trim.json").is_file()

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
    assert inspected["total_episodes"] == 3
    assert inspected["total_frames"] == 12
    assert sum(inspected["videos"].values()) == 12
    for episode in inspected["episodes"]:
        assert episode["length"] == 4
        assert episode["frame_index"] == [0, 1, 2, 3]
        assert episode["episode_index"] == [episode["index"]] * 4
        assert [
            [round(value, 6) for value in row] for row in episode["state"]
        ] == [
            [0.05, 0.1],
            [0.1, 0.2],
            [0.15, 0.3],
            [0.2, 0.4],
        ]
    original = json.loads(
        subprocess.run(
            [python, str(SCENARIO), "inspect", str(path)],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path.cwd())},
        ).stdout
    )
    assert original["valid"] is True
    assert original["total_frames"] == 21
    assert all(episode["length"] == 7 for episode in original["episodes"])


# ------------------------------------------------------- conversion integration
def capture_with_positions(settings_factory, positions, *, session_id="idle-session"):
    """Build an AgileX-shaped capture with explicit joint positions per frame."""

    client = settings_factory("client")
    profile = client.profile()
    root = client.root / "captures" / "idle"
    episode = root / "episode0"
    adapter = MockAdapter(profile)
    fps = profile["output_fps"]
    for camera in profile["cameras"]:
        (episode / "camera/color" / camera["id"]).mkdir(parents=True)
    roles = profile["state_streams"] + profile["action_streams"]
    for role in roles:
        (episode / "arm/jointState" / role).mkdir(parents=True)
    (episode / "instructions.json").write_text(
        json.dumps({"full-instructions": ["fold cloth"]})
    )
    for index, position in enumerate(positions):
        stamp = f"{(index * 1_000_000_000 // fps) / 1e9:.9f}"
        for role in roles:
            (episode / "arm/jointState" / role / f"{stamp}.json").write_text(
                json.dumps({"position": position, "velocity": [], "effort": []})
            )
        for camera in profile["cameras"]:
            (episode / "camera/color" / camera["id"] / f"{stamp}.jpg").write_bytes(
                adapter.frame(camera["id"])
            )
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        files.append(
            {
                "path": str(path.relative_to(root)),
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest = {
        "manifest_version": 1,
        "session_id": session_id,
        "robot_id": client.robot_id,
        "project_id": client.project_id,
        "control_mode": "teleoperation",
        "raw_format": "agilex_timestamped_directory_v1",
        "capture_mode": "mock",
        "instruction": "fold cloth",
        "profile": profile,
        "profile_hash": digest(profile),
        "complete": True,
        "files": files,
    }
    return root, manifest


def idle_capture(settings_factory, *, frames=90, head=20, tail=20):
    """Build an AgileX-shaped capture whose first and last frames never move."""

    active_end = frames - tail
    last_active = round(0.05 * (active_end - head), 6)
    positions = []
    for index in range(frames):
        if index < head:
            position = [0.0] * 7
        elif index < active_end:
            position = [round(0.05 * (index - head + 1), 6)] * 7
        else:
            position = [last_active] * 7
        positions.append(position)
    return capture_with_positions(settings_factory, positions)


def static_interval_capture(
    settings_factory, *, head=60, motion=30, middle=60, session_id="static-session"
):
    """Move, hold still long enough to remove, then move again."""

    positions = [[0.0] * 7 for _ in range(head)]
    level = 0.0
    for index in range(motion):
        level = round(0.05 * (index + 1), 6)
        positions.append([level] * 7)
    positions.extend([list(positions[-1]) for _ in range(middle)])
    for _ in range(motion):
        level = round(level + 0.05, 6)
        positions.append([level] * 7)
    return capture_with_positions(
        settings_factory, positions, session_id=session_id
    )


def upload_raw(ingest, raw, manifest):
    uid = ingest.create(manifest, "default", "")["id"]
    for index, entry in enumerate(manifest["files"]):
        data = (raw / entry["path"]).read_bytes()
        size = ingest.settings.chunk_bytes
        for offset in range(0, len(data), size):
            chunk = data[offset : offset + size]
            ingest.chunk(uid, index, offset, hashlib.sha256(chunk).hexdigest(), chunk)
    ingest.complete(uid)
    return uid


def test_conversion_trim_requires_real_mode_and_a_boolean(settings_factory):
    settings = settings_factory("server")
    app = create_app(settings, background=False)
    auth = admin(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/uploads/missing/process",
            headers=auth,
            json={"trim_idle_frames": True},
        )
        assert response.status_code == 400
        assert "演示模式" in response.text
        assert (
            client.post(
                "/api/uploads/missing/process",
                headers=auth,
                json={"trim_idle_frames": "yes"},
            ).status_code
            == 422
        )
    with pytest.raises(DomainError, match="布尔值"):
        app.state.runner.enqueue("missing", trim_idle_frames=1)


@pytest.mark.parametrize("target_format", ["v2.1", "v3.0"])
def test_real_conversion_trims_boundary_idle_frames(
    settings_factory, letools_binary, target_format
):
    raw, manifest = idle_capture(settings_factory)
    settings = settings_factory(
        "server",
        processing_mode="real",
        letools_bin=letools_binary,
        min_free_bytes=0,
        command_timeout=600,
    )
    app = create_app(settings, background=False)
    ingest = app.state.ingest
    uid = upload_raw(ingest, raw, manifest)
    job = app.state.runner.enqueue(
        uid,
        target_format,
        run_doctor=False,
        trim_idle_frames=True,
    )
    assert job["trim_idle_frames"] is True
    assert app.state.runner.tick() is True
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "CONVERTED", result
    assert result["trim_idle_frames"] is True
    summary = result["trim_summary"]
    assert summary["episodes"] == 1
    assert summary["frames"] == 90
    assert summary["frames_kept"] == 62
    assert summary["frames_removed"] == 28
    assert result["trim_report"]["frames"] == 62
    assert result["trim_policy"]["feature"] == "observation.state"
    assert result["trim_policy"]["remove_fully_static"] is False
    assert result["episode_count"] == 1
    assert result["frame_count"] == 62
    output = Path(result["dataset_path"])
    info = json.loads((output / "meta/info.json").read_text())
    assert info["codebase_version"] == target_format
    assert info["total_episodes"] == 1
    assert info["total_frames"] == 62
    lineage = json.loads(
        (output / "meta/bodypipeline-stationary-trim.json").read_text()
    )
    assert lineage["entry"] == "conversion"
    assert lineage["summary"]["frames_removed"] == 28
    # The raw capture is a separate dataset and is never rewritten by trimming.
    assert all((raw / entry["path"]).is_file() for entry in manifest["files"])


def test_real_conversion_removes_mid_recording_static_intervals(
    settings_factory, letools_binary
):
    raw, manifest = static_interval_capture(settings_factory)
    settings = settings_factory(
        "server",
        processing_mode="real",
        letools_bin=letools_binary,
        min_free_bytes=0,
        command_timeout=600,
    )
    app = create_app(settings, background=False)
    ingest = app.state.ingest
    uid = upload_raw(ingest, raw, manifest)
    job = app.state.runner.enqueue(
        uid,
        "v3.0",
        run_doctor=False,
        trim_static_intervals=True,
    )
    assert job["trim_static_intervals"] is True
    assert job["trim_idle_frames"] is False
    assert app.state.runner.tick() is True
    result = ingest.store.get("job", job["id"])
    assert result["state"] == "CONVERTED", result
    summary = result["interval_summary"]
    assert summary["frames_removed"] > 0
    assert summary["output_episodes"] == 2
    assert result["episode_count"] == 2
    assert result["frame_count"] == summary["frames_kept"]
    assert result["interval_report"]["validated"] is True
    assert result["interval_report"]["frames_removed"] == summary["frames_removed"]
    assert result["interval_policy"]["feature"] == "observation.state"
    assert result["interval_policy"]["min_static_seconds"] == 2.0
    output = Path(result["dataset_path"])
    info = json.loads((output / "meta/info.json").read_text())
    assert info["codebase_version"] == "v3.0"
    assert info["total_episodes"] == 2
    assert info["total_frames"] == summary["frames_kept"]
    lineage = json.loads(
        (output / "meta/bodypipeline-static-intervals.json").read_text()
    )
    assert lineage["entry"] == "conversion"
    assert lineage["algorithm"] == "static-intervals-v1"
    assert lineage["summary"]["frames_removed"] == summary["frames_removed"]
    assert len(lineage["episodes"]) == 2
    # The raw capture is only read, never mutated by the in-conversion trim.
    assert all((raw / entry["path"]).is_file() for entry in manifest["files"])

