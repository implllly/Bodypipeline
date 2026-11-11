import hashlib
import io
import json
import subprocess
from pathlib import Path

import h5py
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from pipeline.server.app import create_app
from pipeline.server.providers import get_provider
from pipeline.server.providers.arx_kinesthetic import (
    ACTION_REPRESENTATION,
    CAMERAS,
    RAW_FORMAT,
    RAW_SCHEMA,
    VALID_ACTION_MASK,
    ARXKinestheticProvider,
)
from pipeline.shared.core import DomainError, digest


@pytest.fixture
def profile():
    return json.loads(Path("configs/arx-x5-kinesthetic-v1.json").read_text())


def metadata(profile):
    return {
        "session_id": "arx-dagger-session",
        "robot_id": "arx-x5-001",
        "project_id": "teleop",
        "instruction": "dagger test",
        "dataset_name": "ARX_DAgger_TEST",
        "task_name": "ARX_DAgger_TEST",
        "collector_name": "tester",
        "started_at": 1000.0,
        "stopped_at": 1001.0,
        "capture_mode": "ros2",
        "profile_hash": digest(profile),
        "schema": RAW_SCHEMA,
        "raw_format": RAW_FORMAT,
        "source": "dagger",
    }


def write_capture(path, profile, *, gap=False):
    times = np.array([0.0, 1 / 30, 2 / 30, 3 / 30, 4 / 30], dtype=np.float64)
    if gap:
        times[3:] += 0.4
    steps = np.concatenate([np.diff(times), [0.0]])
    valid = (steps > 0) & (steps <= 0.25)
    state = np.arange(5 * 14, dtype=np.float32).reshape(5, 14) / 100
    action = np.concatenate([state[1:], state[-1:]], axis=0)
    rows = metadata(profile)
    with h5py.File(path, "w") as handle:
        handle.attrs.update(
            schema=RAW_SCHEMA,
            raw_format=RAW_FORMAT,
            complete=True,
            robot_type="ARX_X5",
            fps=30,
            session_id=rows["session_id"],
            profile_hash=digest(profile),
            task=rows["instruction"],
            label_source=ACTION_REPRESENTATION,
            timebase="recorded_monotonic",
            action_target_offset="next_recorded_frame",
            source_raw_format="enactive_arx_kinesthetic_zip_v1",
        )
        handle.create_dataset("observation.state", data=state)
        handle.create_dataset("action", data=action)
        handle.create_dataset("action_dt", data=steps)
        handle.create_dataset("action_valid_mask", data=valid)
        handle.create_dataset("timestamps/monotonic", data=times + 1000)
        handle.create_dataset("source/state", data=state)
        handle.create_dataset("source/captured_at", data=times + 1000)
        for name in CAMERAS:
            dataset = handle.create_dataset(
                f"observation.images.{name}",
                shape=(len(times),),
                dtype=h5py.vlen_dtype(np.dtype("uint8")),
            )
            dataset.attrs["encoded_format"] = "png"
            for index in range(len(times)):
                output = io.BytesIO()
                Image.new("RGB", (32, 24), (index * 40, 20, 10)).save(
                    output, format="PNG"
                )
                dataset[index] = np.frombuffer(output.getvalue(), dtype=np.uint8)
        handle.create_dataset(
            "metadata", data=json.dumps(rows), dtype=h5py.string_dtype()
        )
    return rows


def manifest(path, profile, rows):
    return {
        **rows,
        "manifest_version": 1,
        "complete": True,
        "control_mode": "teleoperation",
        "profile": profile,
        "files": [
            {
                "path": "episode.hdf5",
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        ],
    }


def runtime_settings(settings_factory, monkeypatch, profile):
    token = "arx-dagger-" + "d" * 32
    monkeypatch.setenv("ARX_DAGGER_TOKEN", token)
    settings = settings_factory(
        "server",
        profiles={
            "arx_kinesthetic": {
                "provider": "arx_kinesthetic",
                "profile": profile,
                "raw_formats": [RAW_FORMAT],
            }
        },
        default_profile="arx_kinesthetic",
        devices=[
            {
                "robot_id": "arx-x5-001",
                "project_id": "teleop",
                "token_env": "ARX_DAGGER_TOKEN",
                "targets": ["default"],
                "runtime_by_raw_format": {RAW_FORMAT: "arx_kinesthetic"},
            }
        ],
    )
    return settings, {"Authorization": "Bearer " + token}


def test_registered_provider_and_profile_hash(profile):
    assert isinstance(get_provider("arx_kinesthetic"), ARXKinestheticProvider)
    assert digest(profile) == (
        "8831ad8f8c9588885fd2de36ab0eba41a21a303cee640ed4f98eb6b38d118557"
    )
    assert {
        blocker["field"]
        for blocker in ARXKinestheticProvider().profile_blockers(profile)
    } == {"semantics_approved", "clock_verified"}
    assert (
        ARXKinestheticProvider().conversion_capabilities(profile)["gripper_closure"][
            "calibration_id"
        ]
        == "arx_x5_nominal_motor_travel_v1"
    )


def test_http_upload_commit_and_dispatch(
    settings_factory, monkeypatch, tmp_path, profile
):
    path = tmp_path / "episode.hdf5"
    rows = write_capture(path, profile)
    payload = path.read_bytes()
    settings, headers = runtime_settings(settings_factory, monkeypatch, profile)
    app = create_app(settings, background=False)
    with TestClient(app) as http:
        response = http.post(
            "/device/uploads",
            headers=headers,
            json={"manifest": manifest(path, profile, rows), "target": "default"},
        )
        assert response.status_code == 201, response.text
        uid = response.json()["id"]
        response = http.put(
            f"/device/uploads/{uid}/files/0?offset=0",
            headers={
                **headers,
                "X-Chunk-SHA256": hashlib.sha256(payload).hexdigest(),
            },
            content=payload,
        )
        assert response.status_code == 200, response.text
        response = http.post(f"/device/uploads/{uid}/complete", headers=headers)
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["state"] == "COMMITTED"
        assert receipt["runtime_id"] == "arx_kinesthetic"
        assert receipt["provider"] == "arx_kinesthetic"
        assert Path(receipt["raw_path"], "episode.hdf5").read_bytes() == payload
        uploads = http.get(
            "/api/uploads", headers={"Authorization": "Bearer " + settings.token}
        ).json()
        row = next(item for item in uploads if item["id"] == uid)
        assert row["robot_type"] == "ARX_X5"
        assert {blocker["field"] for blocker in row["conversion_blockers"]} >= {
            "semantics_approved",
            "clock_verified",
        }


def test_prepare_preserves_next_action_mask_and_png(tmp_path, profile):
    path = tmp_path / "episode.hdf5"
    write_capture(path, profile, gap=True)
    original = path.read_bytes()
    provider = ARXKinestheticProvider()
    report = provider.prepare(path, tmp_path / "aligned", profile)
    assert path.read_bytes() == original
    assert report["source_frames"] == 5
    assert report["invalid_action_labels"] >= 2
    with h5py.File(report["aligned_path"], "r") as aligned:
        assert aligned.attrs["source_format"] == RAW_FORMAT
        assert aligned[VALID_ACTION_MASK].shape == (report["frames"], 1)
        assert not aligned[VALID_ACTION_MASK][-1, 0]
        assert aligned["observation.images.head"].attrs["encoded_format"] == "png"
        source_index = np.asarray(aligned["source_frame_index"])
        assert source_index[0] == 0 and source_index[-1] == 4
        assert np.all(np.diff(source_index) >= 0)
        assert len(np.unique(source_index)) < len(source_index)
    assert provider.quality(Path(report["aligned_path"]), profile)["status"] == "PASS"
    adjusted = provider.prepare(
        path,
        tmp_path / "adjusted",
        profile,
        options={"gripper_closure": {"increase_percent": 10}},
    )
    assert "gripper_closure" in adjusted["action_corrections"]
    # DAgger labels action[t] = state[t + 1], so the detected interval is
    # shifted back by one frame before the action is rewritten.
    assert (
        adjusted["action_corrections"]["gripper_closure"]["action_state_offset"] == 1
    )


@pytest.mark.parametrize(
    "corruption", ["schema", "last_valid", "mask", "action", "metadata", "image"]
)
def test_invalid_dagger_data_is_rejected(tmp_path, profile, corruption):
    path = tmp_path / "episode.hdf5"
    rows = write_capture(path, profile)
    with h5py.File(path, "r+") as handle:
        if corruption == "schema":
            handle.attrs["schema"] = "bad"
        elif corruption == "last_valid":
            mask = np.asarray(handle["action_valid_mask"])
            mask[-1] = True
            handle["action_valid_mask"][:] = mask
        elif corruption == "mask":
            mask = np.asarray(handle["action_valid_mask"])
            mask[1] = not mask[1]
            handle["action_valid_mask"][:] = mask
        elif corruption == "action":
            handle["action"][1, 0] += 1
        elif corruption == "metadata":
            handle["metadata"][()] = json.dumps({**rows, "session_id": "other"})
        else:
            handle["observation.images.head"][2] = np.frombuffer(
                b"not-an-image", dtype=np.uint8
            )
    with pytest.raises(DomainError):
        ARXKinestheticProvider().episode_metadata(path, profile)


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_letools_conversion_with_action_mask(
    tmp_path, profile, version, letools_binary
):
    path = tmp_path / "episode.hdf5"
    write_capture(path, profile)
    provider = ARXKinestheticProvider()
    source = tmp_path / "aligned"
    destination = tmp_path / "dataset"
    provider.prepare(path, source / "episode0", profile)
    args = provider.conversion_args(
        executable=letools_binary,
        source=source,
        destination=destination,
        profile=profile,
        job={"instruction": "dagger test", "target_format": version},
    )
    result = subprocess.run(args, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
    provider.patch_dataset_info(destination, profile)
    info = json.loads((destination / "meta/info.json").read_text())
    assert info["codebase_version"] == version
    assert info["robot_type"] == "ARX_X5"
    assert info["total_frames"] == 5
    assert info["features"][VALID_ACTION_MASK]["names"] == ["valid"]
    assert info["action_semantics"]["target_offset"] == "next_recorded_frame"
    assert len(list(destination.rglob("*.mp4"))) == 3
