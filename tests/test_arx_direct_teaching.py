import copy
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
from pipeline.server.providers.arx_direct_teaching import (
    CAMERAS,
    RAW_FORMAT,
    RAW_SCHEMA,
    SOURCES,
    ARXDirectTeachingProvider,
    capture_contract_hash,
)
from pipeline.shared.core import DomainError, digest


@pytest.fixture
def profile():
    value = json.loads(Path("configs/arx-x5-direct-teaching-v1.json").read_text())
    value["minimum_frames"] = 2
    return value


def write_capture(path, profile, *, gap=False, gap_seconds=1.0):
    n = 4
    times = np.arange(n, dtype=np.float64) / 30
    if gap:
        times[2:] += gap_seconds
    exact = 1793070494000000000 + np.round(times * 1e9).astype(np.int64)
    metadata = dict(
        session_id="arx-test-session",
        robot_id="arx-x5-001",
        project_id="teleop",
        dataset_name="test-arx",
        task_name="test",
        collector_name="tester",
        instruction="",
        profile_hash=digest(profile),
        schema=RAW_SCHEMA,
        raw_format=RAW_FORMAT,
    )
    with h5py.File(path, "w") as h:
        h.attrs.update(
            schema=RAW_SCHEMA,
            raw_format=RAW_FORMAT,
            complete=True,
            profile_hash=digest(profile),
            capture_contract_hash=capture_contract_hash(profile),
            fps=30,
            frame_count=n,
            session_id=metadata["session_id"],
            recording_mode="raw_first",
            alignment_clock="source",
        )
        values = np.arange(n * 14, dtype=np.float32).reshape(n, 14) / 100
        for key in (
            "observations/qpos",
            "observations/qvel",
            "observations/effort",
            "observations/eef",
            "action",
            "action_eef",
        ):
            h.create_dataset(key, data=values)
        h.create_dataset("timestamps/monotonic", data=times + 1000)
        h.create_dataset("timestamps/reference_source_ns", data=exact)
        source = np.repeat(exact[:, None], 5, axis=1)
        h.create_dataset("timestamps/source_ns", data=source)
        h.create_dataset("timestamps/received_monotonic_ns", data=source)
        h.create_dataset(
            "source_sample_id", data=np.repeat(np.arange(1, n + 1)[:, None], 5, axis=1)
        )
        for i, name in enumerate(SOURCES):
            h.create_dataset(
                f"timestamps/source/{name}", data=source[:, i].astype(np.float64) / 1e9
            )
        for name in CAMERAS:
            ds = h.create_dataset(
                f"observations/images/{name}",
                shape=(n,),
                dtype=h5py.vlen_dtype(np.dtype("uint8")),
            )
            for index in range(n):
                payload = io.BytesIO()
                Image.new("RGB", (32, 24), (index * 50, 40, 60)).save(
                    payload, format="JPEG"
                )
                ds[index] = np.frombuffer(payload.getvalue(), dtype=np.uint8)
        h.create_dataset(
            "metadata", data=json.dumps(metadata), dtype=h5py.string_dtype()
        )
    return metadata


def manifest(path, profile, metadata):
    return {
        **metadata,
        "manifest_version": 1,
        "complete": True,
        "control_mode": "teleoperation",
        "capture_mode": "ros2",
        "profile": profile,
        "files": [
            {
                "path": "episode.hdf5",
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        ],
    }


def server(settings_factory, monkeypatch, profile):
    token = "arx-test-" + "a" * 32
    monkeypatch.setenv("ARX_TEST_TOKEN", token)
    settings = settings_factory(
        "server",
        profiles={
            "arx_test": {
                "provider": "arx_direct_teaching",
                "profile": profile,
                "raw_formats": [RAW_FORMAT],
            }
        },
        default_profile="arx_test",
        devices=[
            {
                "robot_id": "arx-x5-001",
                "project_id": "teleop",
                "token_env": "ARX_TEST_TOKEN",
                "runtime_id": "arx_test",
                "targets": ["default"],
            }
        ],
    )
    return settings, {"Authorization": "Bearer " + token}


def test_http_resume_commit_and_persistence(
    settings_factory, monkeypatch, tmp_path, profile
):
    path = tmp_path / "episode.hdf5"
    metadata = write_capture(path, profile)
    m = manifest(path, profile, metadata)
    data = path.read_bytes()
    settings, headers = server(settings_factory, monkeypatch, profile)
    with TestClient(create_app(settings, background=False)) as http:
        response = http.post(
            "/device/uploads",
            headers=headers,
            json={"manifest": m, "target": "default"},
        )
        assert response.status_code == 201, response.text
        uid = response.json()["id"]
        chunk = data[:100]
        response = http.put(
            f"/device/uploads/{uid}/files/0?offset=0",
            content=chunk,
            headers={**headers, "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
        )
        assert response.status_code == 200
        assert (
            http.post(f"/device/uploads/{uid}/complete", headers=headers).status_code
            == 422
        )
    with TestClient(create_app(settings, background=False)) as http:
        assert http.get(f"/device/uploads/{uid}", headers=headers).json()[
            "offsets"
        ] == [100]
        chunk = data[100:]
        assert (
            http.put(
                f"/device/uploads/{uid}/files/0?offset=100",
                content=chunk,
                headers={
                    **headers,
                    "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest(),
                },
            ).status_code
            == 200
        )
        response = http.post(f"/device/uploads/{uid}/complete", headers=headers)
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["state"] == "COMMITTED"
        assert receipt["manifest_hash"] == digest(m)
        assert receipt["runtime_id"] == "arx_test"
        assert receipt["robot_id"] == "arx-x5-001"
        assert Path(receipt["raw_path"], "episode.hdf5").read_bytes() == data
        assert (
            http.post(f"/device/uploads/{uid}/complete", headers=headers).json()
            == receipt
        )
        rows = http.get(
            "/api/uploads", headers={"Authorization": "Bearer " + settings.token}
        ).json()
        assert any(row["id"] == uid and not row["conversion_blockers"] for row in rows)


def test_trusted_import_accepts_original_episode_basenames(
    settings_factory, monkeypatch, tmp_path, profile
):
    path = tmp_path / "episode1.hdf5"
    write_capture(path, profile)
    with h5py.File(path, "r+") as handle:
        handle.attrs["capture_contract_hash"] = "legacy-client-contract"
        del handle["metadata"]
    settings, _ = server(settings_factory, monkeypatch, profile)
    settings.import_roots = [tmp_path]
    app = create_app(settings, background=False)
    admin = {"Authorization": "Bearer " + settings.token}
    with TestClient(app) as http:
        response = http.post(
            "/api/imports",
            headers=admin,
            json={"path": str(tmp_path), "runtime_id": "arx_test"},
        )
        assert response.status_code == 201, response.text
        result = response.json()
        assert result["episode_count"] == 1
        assert result["provider"] == "arx_direct_teaching"
        rows = http.get("/api/uploads", headers=admin).json()
        imported = next(
            row for row in rows if row["collection_id"] == result["collection_id"]
        )
        assert app.state.ingest.manifest(imported)["raw_format"] == RAW_FORMAT
        assert imported["state"] == "COMMITTED"
        report = ARXDirectTeachingProvider().prepare(
            path, tmp_path / "aligned", profile
        )
        assert report["capture_contract_match"] is False


@pytest.mark.parametrize(
    "change", ["robot", "format", "hash", "filename", "multiple", "token"]
)
def test_upload_binding_rejections(
    settings_factory, monkeypatch, tmp_path, profile, change
):
    path = tmp_path / "episode.hdf5"
    metadata = write_capture(path, profile)
    m = manifest(path, profile, metadata)
    settings, headers = server(settings_factory, monkeypatch, profile)
    if change == "robot":
        m["robot_id"] = "agx-001"
    elif change == "format":
        m["raw_format"] = "aloha_hdf5_episodes_v1"
    elif change == "hash":
        m["profile"] = {**profile, "notes": "changed"}
        m["profile_hash"] = digest(m["profile"])
    elif change == "filename":
        m["files"][0]["path"] = "episode0/episode.hdf5"
    elif change == "multiple":
        m["files"].append(copy.deepcopy(m["files"][0]))
    else:
        headers = {"Authorization": "Bearer invalid"}
    with TestClient(create_app(settings, background=False)) as http:
        assert http.post(
            "/device/uploads",
            headers=headers,
            json={"manifest": m, "target": "default"},
        ).status_code in {400, 401, 403, 422}


@pytest.mark.parametrize(
    "corruption",
    [
        "schema",
        "complete",
        "profile_hash",
        "capture_contract_hash",
        "action",
        "link",
        "metadata",
        "late_jpeg",
        "timestamps",
    ],
)
def test_invalid_raw_cannot_commit(
    settings_factory, monkeypatch, tmp_path, profile, corruption
):
    path = tmp_path / "episode.hdf5"
    metadata = write_capture(path, profile)
    with h5py.File(path, "r+") as h:
        if corruption in {"schema", "profile_hash", "capture_contract_hash"}:
            h.attrs[corruption] = "bad"
        elif corruption == "complete":
            h.attrs["complete"] = False
        elif corruption == "action":
            h["action"][1, 0] = 99
        elif corruption == "link":
            h["external"] = h5py.ExternalLink("/etc/passwd", "/")
        elif corruption == "metadata":
            changed = {**metadata, "robot_id": "other"}
            h["metadata"][()] = json.dumps(changed)
        elif corruption == "late_jpeg":
            h["observations/images/head"][3] = np.frombuffer(
                b"invalid JPEG", dtype=np.uint8
            )
        else:
            h["timestamps/reference_source_ns"][2] = 1
    m = manifest(path, profile, metadata)
    data = path.read_bytes()
    settings, headers = server(settings_factory, monkeypatch, profile)
    with TestClient(create_app(settings, background=False)) as http:
        response = http.post(
            "/device/uploads",
            headers=headers,
            json={"manifest": m, "target": "default"},
        )
        uid = response.json()["id"]
        assert (
            http.put(
                f"/device/uploads/{uid}/files/0?offset=0",
                content=data,
                headers={**headers, "X-Chunk-SHA256": hashlib.sha256(data).hexdigest()},
            ).status_code
            == 200
        )
        assert (
            http.post(f"/device/uploads/{uid}/complete", headers=headers).status_code
            == 422
        )
        assert (
            http.get(f"/device/uploads/{uid}", headers=headers).json()["state"]
            == "UPLOADING"
        )


def test_alignment_preserves_source_and_does_not_approve(profile, tmp_path):
    path = tmp_path / "episode.hdf5"
    write_capture(path, profile)
    original = path.read_bytes()
    provider = ARXDirectTeachingProvider()
    report = provider.prepare(path, tmp_path / "aligned", profile)
    json.dumps(report)
    assert report["frames"] == 4
    assert report["alignment_clock"] == "source"
    assert path.read_bytes() == original
    with h5py.File(report["aligned_path"], "r") as h:
        np.testing.assert_allclose(h["timestamp"][:], np.arange(4) / 30)
        np.testing.assert_array_equal(h["source_frame_index"][:], np.arange(4))
        np.testing.assert_array_equal(h["action"][:], h["observation.state"][:])
    assert provider.profile_blockers(profile) == []
    unapproved = {
        **profile,
        "semantics_approved": False,
        "clock_verified": False,
    }
    assert {b["field"] for b in provider.profile_blockers(unapproved)} == {
        "semantics_approved",
        "clock_verified",
    }


def test_server_conversion_config_does_not_change_capture_contract(profile, tmp_path):
    changed = copy.deepcopy(profile)
    changed["conversion"]["gripper_closure"]["default_increase_percent"] = 15
    changed["max_capture_gap_ms"] = 500
    assert capture_contract_hash(changed) == capture_contract_hash(profile)
    provider = ARXDirectTeachingProvider()
    assert provider.conversion_capabilities(profile)["gripper_closure"] == {
        "correction": "additive_shift_v1",
        "minimum_increase_percent": 0,
        "maximum_increase_percent": 100,
        "default_increase_percent": 10,
        "algorithm": "closing_hold_plateaus_v4",
        "calibration_id": "arx_x5_nominal_motor_travel_v1",
        "percentage_definition": "nominal_motor_travel",
    }
    path = tmp_path / "episode.hdf5"
    write_capture(path, profile)
    report = provider.prepare(
        path,
        tmp_path / "adjusted",
        profile,
        options={"gripper_closure": {"increase_percent": 10}},
    )
    assert "gripper_closure" in report["action_corrections"]


def test_long_capture_gaps_are_not_filled(profile, tmp_path):
    path = tmp_path / "episode.hdf5"
    write_capture(path, profile, gap=True)
    with pytest.raises(DomainError, match="间隙"):
        ARXDirectTeachingProvider().prepare(path, tmp_path / "aligned", profile)


def test_sensor_skew_is_reported_without_rejecting_aligned_capture(profile, tmp_path):
    path = tmp_path / "episode.hdf5"
    write_capture(path, profile)
    with h5py.File(path, "r+") as handle:
        source = np.asarray(handle["timestamps/source_ns"]).copy()
        source[:, 4] += 100_000_000
        handle["timestamps/source_ns"][:] = source
        handle["timestamps/source/right_arm"][:] = source[:, 4].astype(np.float64) / 1e9
    provider = ARXDirectTeachingProvider()
    report = provider.prepare(path, tmp_path / "aligned", profile)
    json.dumps(report)
    assert report["sensor_skew_exceeded"] is True
    assert report["quality_warnings"]
    quality = provider.quality(Path(report["aligned_path"]), profile)
    assert quality["status"] == "WARN"
    assert quality["max_sensor_skew_ms"] > profile["max_sensor_skew_ms"]


def test_bounded_capture_gap_is_repeated_with_warning(profile, tmp_path):
    path = tmp_path / "episode.hdf5"
    write_capture(path, profile, gap=True, gap_seconds=0.06)
    provider = ARXDirectTeachingProvider()
    report = provider.prepare(path, tmp_path / "aligned", profile)
    json.dumps(report)
    assert report["capture_gap_exceeded"] is True
    assert report["max_capture_gap_ms"] > 2 * profile["max_frame_skew_ms"]
    assert report["quality_warnings"]
    assert provider.quality(Path(report["aligned_path"]), profile)["status"] == "WARN"


def test_resampling_uses_source_clock_instead_of_row_numbers(profile, tmp_path):
    path = tmp_path / "episode.hdf5"
    write_capture(path, profile)
    with h5py.File(path, "r+") as h:
        reference = 1793070494000000000 + np.array([0, 10000000, 34000000, 67000000])
        source = np.repeat(reference[:, None], 5, axis=1)
        h["timestamps/reference_source_ns"][:] = reference
        h["timestamps/source_ns"][:] = source
        h["timestamps/received_monotonic_ns"][:] = source
        for i, name in enumerate(SOURCES):
            h[f"timestamps/source/{name}"][:] = source[:, i].astype(np.float64) / 1e9
    report = ARXDirectTeachingProvider().prepare(path, tmp_path / "aligned", profile)
    assert report["source_frames"] == 4
    assert report["frames"] == 3
    with h5py.File(report["aligned_path"], "r") as h:
        np.testing.assert_array_equal(h["source_frame_index"][:], [0, 2, 3])


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_letools_mapping(profile, tmp_path, version):
    path = tmp_path / "episode.hdf5"
    write_capture(path, profile)
    provider = ARXDirectTeachingProvider()
    source = tmp_path / "aligned"
    provider.prepare(path, source / "episode0", profile)
    args = provider.conversion_args(
        executable="letools",
        source=source,
        destination=tmp_path / "dataset",
        profile=profile,
        job={"instruction": "test", "target_format": version},
    )
    assert args[args.index("--to") + 1] == version
    preset = json.loads(Path(args[args.index("--preset") + 1]).read_text())
    assert preset["mapping"]["robot_type"] == "ARX_X5"
    assert len(preset["mapping"]["numeric_fields"][0]["names"]) == 14
    assert len(preset["mapping"]["video_fields"]) == 3


@pytest.mark.parametrize("version", ["v2.1", "v3.0"])
def test_real_letools_conversion(profile, tmp_path, version, letools_binary):
    path = tmp_path / "episode.hdf5"
    write_capture(path, profile)
    provider = ARXDirectTeachingProvider()
    source, destination = tmp_path / "aligned", tmp_path / "dataset"
    provider.prepare(path, source / "episode0", profile)
    args = provider.conversion_args(
        executable=letools_binary,
        source=source,
        destination=destination,
        profile=profile,
        job={"instruction": "test", "target_format": version},
    )
    result = subprocess.run(args, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
    provider.patch_dataset_info(destination, profile)
    info = json.loads((destination / "meta/info.json").read_text())
    assert info["total_episodes"] == 1
    assert info["total_frames"] == 4
    assert info["codebase_version"] == version
    assert info["robot_type"] == "ARX_X5"
    assert info["features"]["action"]["names"] == profile["joint_names"]
    assert len(list(destination.rglob("*.mp4"))) == 3
