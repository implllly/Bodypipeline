"""Tianji capture ownership, immutable packaging, trusted routing and QC."""

import io
import json
from pathlib import Path

import h5py
import httpx
import numpy as np
import pytest
from PIL import Image

from pipeline.client.adapters.apex import ApexAdapter
from pipeline.client.adapters.registry import create_adapter
from pipeline.client.packagers import TianjiMCAPPackager
from pipeline.server.ingest import IngestService
from pipeline.server.providers.tianji import TianjiProvider
from pipeline.shared.core import DomainError, digest
from pipeline.shared.tianji import (
    CAMERAS,
    TIANJI_ALIGNED_SCHEMA,
    TIANJI_MCAP_FORMAT,
    recording_files,
)


def profile():
    return json.loads(Path("configs/tianji-marvin-pro-v1.json").read_text())


def recording(path):
    for part in ("data", "video"):
        (path / part).mkdir(parents=True)
        (path / part / "metadata.yaml").write_text("fixture metadata")
        (path / part / "bag_0.mcap").write_bytes(
            b"fixture raw mcap: not real robot data"
        )
    return path


def setup_apex(settings_factory, tmp_path, *, timeout=False, drain=True):
    storage = tmp_path / "apex"
    storage.mkdir()
    original = recording(storage / "session")
    settings = settings_factory(
        mode="ros2",
        capture_adapter="apex",
        capture_packager="tianji_mcap",
        profiles={
            "tianji": dict(
                profile=profile(), provider="tianji", raw_formats=[TIANJI_MCAP_FORMAT]
            )
        },
        default_profile="tianji",
        capture_options=dict(
            allow_recording=True,
            project_id="dedicated",
            storage_root=str(storage),
            timeout_s=0.01,
        ),
    )
    state = dict(phase="idle")
    calls = []

    def handler(request):
        route = request.url.path.rsplit("/", 1)[-1]
        calls.append((request.method, route))
        if request.method == "GET" and route == "projects":
            data = dict(
                projects=[
                    dict(id="dedicated", available=True, storage_dir=str(storage))
                ]
            )
        elif request.method == "GET" and route == "status":
            data = state.copy()
        elif request.method == "POST" and route == "start":
            body = json.loads(request.content)
            state.update(
                phase="recording",
                session_id="apex-owned",
                request_id=body["request_id"],
                data_state="recording",
                video_state="recording",
                path=str(original),
            )
            if timeout:
                raise httpx.ReadTimeout("Acknowledgement lost")
            data = state.copy()
        elif request.method == "POST" and route == "stop":
            assert json.loads(request.content) == dict(session_id="apex-owned")
            state.update(
                phase="saved",
                data_state="closed",
                video_state="closed" if drain else "saving",
            )
            data = state.copy()
        else:
            pytest.fail("Unexpected API call: " + str(request.url))
        return httpx.Response(200, json=dict(ok=True, data=data))

    adapter = ApexAdapter(
        settings,
        http=httpx.Client(
            base_url="http://127.0.0.1:8080", transport=httpx.MockTransport(handler)
        ),
    )
    return adapter, settings, state, calls, original


def test_owned_start_stop_and_immutable_package(settings_factory, tmp_path):
    adapter, s, state, calls, original = setup_apex(settings_factory, tmp_path)
    raw = tmp_path / "managed"
    before = {
        p.relative_to(original): p.read_bytes() for p in recording_files(original)
    }
    adapter.start("pipeline-session", raw, "task")
    adapter.start("pipeline-session", raw, "task")
    assert calls.count(("POST", "start")) == 1
    adapter.stop("pipeline-session")
    adapter.stop("pipeline-session")
    assert calls.count(("POST", "stop")) == 1
    sealed = TianjiMCAPPackager().seal(raw, s.profile(), {})
    assert len(sealed["files"]) == 5
    for name, content in before.items():
        assert (original / name).read_bytes() == content
        assert (raw / "episode0" / name).read_bytes() == content
    adapter.close()
    assert {route for method, route in calls if method == "POST"} == {"start", "stop"}


def test_lost_start_ack_reconciles_without_repost(settings_factory, tmp_path):
    adapter, s, state, calls, original = setup_apex(
        settings_factory, tmp_path, timeout=True
    )
    raw = tmp_path / "managed"
    with pytest.raises(DomainError, match="unknown"):
        adapter.start("pipeline-session", raw, "task")
    adapter.start("pipeline-session", raw, "task")
    adapter.stop("pipeline-session")
    assert calls.count(("POST", "start")) == 1


def test_owner_change_never_stops_other_recording(settings_factory, tmp_path):
    adapter, s, state, calls, original = setup_apex(settings_factory, tmp_path)
    adapter.start("pipeline-session", tmp_path / "managed", "task")
    state["session_id"] = "someone-else"
    with pytest.raises(DomainError, match="another"):
        adapter.stop("pipeline-session")
    adapter.close()
    assert ("POST", "stop") not in calls


def test_restart_can_resolve_lost_ack_and_stop_own_session(settings_factory, tmp_path):
    adapter, settings, state, calls, original = setup_apex(
        settings_factory, tmp_path, timeout=True
    )
    raw = tmp_path / "managed"
    with pytest.raises(DomainError):
        adapter.start("pipeline-session", raw, "task")
    restarted = ApexAdapter(settings, http=adapter.http)
    restarted.stop("pipeline-session")
    assert (raw / "episode0/data/bag_0.mcap").is_file()
    assert calls.count(("POST", "start")) == 1


def test_partial_copy_does_not_overwrite_or_seal(settings_factory, tmp_path):
    adapter, settings, state, calls, original = setup_apex(settings_factory, tmp_path)
    raw = tmp_path / "managed"
    adapter.start("pipeline-session", raw, "task")
    (raw / ".copying").mkdir(parents=True)
    (raw / ".copying/partial").write_text("keep for recovery")
    with pytest.raises(DomainError, match="refusing overwrite"):
        adapter.stop("pipeline-session")
    assert not (raw / "episode0").exists()
    assert (raw / ".copying/partial").read_text() == "keep for recovery"


def test_stale_preview_is_not_reported_as_live(settings_factory, tmp_path):
    adapter, settings, state, calls, original = setup_apex(settings_factory, tmp_path)
    settings.capture_options["preview_root"] = str(tmp_path / "preview")
    preview = Path(settings.capture_options["preview_root"])
    preview.mkdir()
    (preview / "head.jpg").write_bytes(b"stale image")
    (preview / "latest.json").write_text(
        json.dumps(
            dict(source_time=1, received_time=1, files=dict(head_left="head.jpg"))
        )
    )
    assert adapter.frame("head_left") is None


def test_writer_not_drained_cannot_seal(settings_factory, tmp_path):
    adapter, s, state, calls, original = setup_apex(
        settings_factory, tmp_path, drain=False
    )
    raw = tmp_path / "managed"
    adapter.start("pipeline-session", raw, "task")
    with pytest.raises(DomainError, match="writer state unknown"):
        adapter.stop("pipeline-session")
    assert not (raw / "episode0").exists()


def test_disabled_health_and_close_are_read_only(settings_factory, tmp_path):
    adapter, s, state, calls, original = setup_apex(settings_factory, tmp_path)
    s.capture_options["allow_recording"] = False
    assert not adapter.health()["ready"]
    with pytest.raises(DomainError, match="disabled"):
        adapter.start("blocked", tmp_path / "managed", "task")
    adapter.close()
    assert all(method == "GET" for method, route in calls)


def test_symlink_and_missing_video_rejected(tmp_path):
    root = recording(tmp_path / "recording")
    (root / "link").symlink_to(root / "data/bag_0.mcap")
    with pytest.raises(DomainError, match="symbolic"):
        recording_files(root)
    (root / "link").unlink()
    (root / "video/bag_0.mcap").unlink()
    with pytest.raises(DomainError, match="missing video"):
        recording_files(root)


def test_mismatched_adapter_fails_closed(settings_factory):
    with pytest.raises(ValueError, match="paired"):
        create_adapter(settings_factory(mode="ros2", capture_adapter="apex"))


def aligned_fixture(path):
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), (12, 34, 56)).save(buffer, format="PNG")
    with h5py.File(path, "w") as f:
        f.attrs.update(schema=TIANJI_ALIGNED_SCHEMA, frame_count=4, fps=50)
        for key, shape in {
            "action": (16,),
            "observation.state": (16,),
            "observation.eef": (14,),
            "observation.wrench_windows": (4, 12),
        }.items():
            f[key] = np.full((4, *shape), 500, dtype=np.float32)  # No soft-limit gate.
        f["timestamp"] = np.arange(4, dtype=np.float32) / 50
        for camera in CAMERAS:
            ds = f.create_dataset(
                "observation.images." + camera,
                (4,),
                dtype=h5py.vlen_dtype(np.dtype("uint8")),
            )
            ds.attrs.update(width=32, height=24, encoded_format="png")
            for i in range(4):
                ds[i] = np.frombuffer(buffer.getvalue(), dtype=np.uint8)
    return path


def test_quality_and_lossless_preset_preserve_auxiliary_features(tmp_path):
    provider = TianjiProvider()
    source = tmp_path / "aligned"
    source.mkdir()
    episode = aligned_fixture(source / "episode0.hdf5")
    assert provider.quality(episode, profile())["status"] == "PASS"
    args = provider.conversion_args(
        executable="letools",
        source=source,
        destination=tmp_path / "dataset",
        profile=profile(),
        job=dict(instruction="fixture"),
    )
    mapping = json.loads(Path(args[args.index("--preset") + 1]).read_text())["mapping"]
    assert len(mapping["video_fields"]) == 4
    assert {x["encoded_format"] for x in mapping["video_fields"]} == {"png"}
    assert len(mapping["numeric_fields"]) == 4
    with h5py.File(episode, "a") as f:
        f["action"][0, 0] = np.nan
    with pytest.raises(DomainError, match="numeric"):
        provider.quality(episode, profile())


def test_tianji_upload_and_runtime_isolation(settings_factory, tmp_path):
    s = settings_factory(
        "server",
        profiles={
            "tianji": dict(
                profile=profile(), provider="tianji", raw_formats=[TIANJI_MCAP_FORMAT]
            )
        },
        default_profile="tianji",
    )
    ingest = IngestService(s)
    raw = tmp_path / "raw"
    recording(raw / "episode0")
    files = TianjiMCAPPackager().seal(raw, profile(), {})["files"]
    manifest = dict(
        manifest_version=1,
        complete=True,
        session_id="tianji-fixture",
        robot_id=s.robot_id,
        project_id=s.project_id,
        control_mode="teleoperation",
        capture_mode="ros2",
        instruction="fixture",
        profile=profile(),
        profile_hash=digest(profile()),
        raw_format=TIANJI_MCAP_FORMAT,
        files=files,
    )
    runtime = ingest.catalog.default()
    row = ingest.create(manifest, "default", "", runtime=runtime)
    for i, spec in enumerate(files):
        data = (raw / spec["path"]).read_bytes()
        ingest.chunk(row["id"], i, 0, spec["sha256"], data)
    receipt = ingest.complete(row["id"])
    assert receipt["state"] == "COMMITTED"
    manifest["raw_format"] = "agilex_joint_camera_hdf5_v1"
    with pytest.raises(DomainError, match="raw_format"):
        ingest.validate_manifest(manifest, runtime=runtime)
    manifest["raw_format"] = TIANJI_MCAP_FORMAT
    manifest["profile"]["robot_type"] = "ARX_X5"
    manifest["profile_hash"] = digest(manifest["profile"])
    with pytest.raises(DomainError, match="robot_type"):
        ingest.validate_manifest(manifest, runtime=runtime)
