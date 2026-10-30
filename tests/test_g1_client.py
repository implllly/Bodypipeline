"""No hardware: durable native episodes, hand-button races, raw upload contract."""

import copy
import json
from pathlib import Path

import pytest
from test_client import request
from test_g1_sonic_provider import write_episode

from pipeline.client.adapters.g1_sonic import STATES, episode_observation
from pipeline.client.resources import WorkPaused
from pipeline.client.service import CaptureService
from pipeline.shared.core import DomainError, atomic_json, digest


class NativeAdapter:
    capabilities = {
        "external_trigger": True,
        "raw_format": "g1_sonic_raw_v1",
        "packager": "g1_sonic_raw",
    }

    def __init__(self):
        self.session = None
        self.current = "idle"

    def begin(self, batch, persist):
        self.session = {"output": str(Path(batch["raw_root"]) / "native")}
        Path(self.session["output"]).mkdir(parents=True)
        persist(self.session)

    def recover(self, batch):
        self.session = batch.get("adapter_session")

    def release(self):
        self.session = None

    def status(self):
        return {"state": STATES[self.current]}

    def end(self):
        if self.current != "idle":
            raise DomainError("请先用手柄结束录制")

    def observe(self):
        if not self.session:
            return []
        observations = []
        for path in Path(self.session["output"]).glob("episodes/*/episode.json"):
            ep = json.loads(path.read_text())
            observations.append(
                {
                    "key": str(path),
                    **episode_observation(ep),
                    "metadata_path": str(path),
                    "source_directory": str(path.parent),
                    "robot_config_path": str(path.parents[2] / "robot_config.json"),
                }
            )
        return observations

    def close(self):
        pass


@pytest.fixture
def native(settings_factory, tmp_path):
    profile = json.loads(Path("configs/g1-sonic11-brainco-v1.json").read_text())
    profile["cameras"] = [
        {"id": "egocentric", "source_key": "ego_view", "width": 32, "height": 24}
    ]
    profile["output_fps"] = 10
    s = settings_factory(
        mode="external",
        profiles={
            "g1": {
                "profile": profile,
                "provider": "g1_sonic",
                "raw_formats": ["g1_sonic_raw_v1"],
            }
        },
        default_profile="g1",
        background_transfer={"enabled": False},
    )
    adapter = NativeAdapter()
    return CaptureService(s, adapter), adapter


def episode(adapter, index=0, state="saved"):
    root = Path(adapter.session["output"]) / "episodes" / f"episode_{index:06d}"
    packet, ep = write_episode(root)
    ep.update(episode_index=index, state=state)
    atomic_json(root / "episode.json", ep)
    return packet, ep


def test_buttons_register_fast_episodes_once_and_discard_never_uploads(native):
    service, adapter = native
    batch = service.begin_batch(request())
    assert batch["state"] == "ACTIVE"
    assert service.begin_batch(request())["id"] == batch["id"]
    episode(adapter, 0)
    episode(adapter, 1, "discarded")
    episode(adapter, 2, "interrupted")
    service.sync_external()
    rows = service.store.all("capture")
    assert len(rows) == 3
    assert {r["state"] for r in rows} == {"SEALING", "DISCARDED", "REVIEW"}
    saved = next(r for r in rows if r["state"] == "SEALING")
    assert service.seal(saved["id"])["state"] == "SEALING"
    assert "作业" in service.store.get("capture", saved["id"])["background_wait"]
    restarted = CaptureService(service.settings, NativeAdapter())
    restarted.recover()
    restarted.sync_external()
    assert len(restarted.store.all("capture")) == 3
    assert restarted.end_batch(batch["id"])["state"] == "ENDED"
    assert restarted.seal_pending()
    saved = restarted.store.get("capture", saved["id"])
    assert saved["state"] == "SEALED"
    manifest = json.loads(
        (Path(saved["raw_root"]).parent / "manifest.json").read_text()
    )
    assert manifest["capture_mode"] == "real"
    assert manifest["raw_format"] == "g1_sonic_raw_v1"
    assert {f["path"] for f in manifest["files"]} == {
        "episode0/packets.raw",
        "episode0/episode.json",
    }
    assert saved["upload_sources"]["episode0/packets.raw"].endswith(
        "episode_000000/packets.raw"
    )
    assert all(
        r["upload_state"] == "NONE"
        for r in restarted.store.all("capture")
        if r["state"] != "SEALED"
    )
    assert not list(Path(batch["raw_root"]).parent.rglob("*.hdf5"))


def test_hold_and_saving_do_not_finalize_or_release_work(native):
    service, adapter = native
    batch = service.begin_batch(request())
    packet, ep = episode(adapter, state="recording")
    for source_state, client_state in [
        ("recording", "RECORDING"),
        ("paused", "PAUSED"),
        ("need_to_save", "SAVING"),
    ]:
        adapter.current = source_state
        ep["state"] = source_state
        atomic_json(packet.parent / "episode.json", ep)
        service.sync_external()
        assert service.store.all("capture")[0]["state"] == client_state
        with pytest.raises(WorkPaused):
            service.background.checkpoint()
        assert service.end_batch(batch["id"])["state"] == "RECOVERY_REQUIRED"
    adapter.current = "idle"
    ep["state"] = "saved"
    atomic_json(packet.parent / "episode.json", ep)
    service.sync_external()
    assert service.end_batch(batch["id"])["state"] == "ENDED"
    assert len(service.store.all("capture")) == 1


def test_raw_changed_after_save_fails_sealing(native):
    service, adapter = native
    batch = service.begin_batch(request())
    packet, _ = episode(adapter)
    service.sync_external()
    assert service.end_batch(batch["id"])["state"] == "ENDED"
    with packet.open("r+b") as f:
        f.seek(-1, 2)
        f.write(b"!")
    service.seal_pending()
    row = service.store.all("capture")[0]
    assert row["state"] == "REVIEW" and row["capture_failed"]
    assert row["upload_state"] == "NONE"


def test_batch_idempotency_and_unknown_binding(native, settings_factory):
    service, _ = native
    service.begin_batch(request())
    changed = request()
    changed["instruction"] = "different"
    with pytest.raises(DomainError):
        service.begin_batch(changed)
    with pytest.raises(DomainError):
        CaptureService(
            settings_factory(mode="external", capture={"adapter": "unknown"})
        )


def test_failed_final_reconciliation_keeps_gate_closed(native):
    service, adapter = native
    batch = service.begin_batch(request())
    packet, ep = episode(adapter)
    service.sync_external()
    ep["packet_count"] += 1
    atomic_json(packet.parent / "episode.json", ep)
    assert service.end_batch(batch["id"])["state"] == "RECOVERY_REQUIRED"
    with pytest.raises(WorkPaused):
        service.background.checkpoint()


class Sampler:
    def __init__(self):
        self.value = {
            "memory_available_ratio": 0.5,
            "cpu_percent": 20,
            "busiest_core_percent": 30,
            "swapout_pages_per_second": 0,
            "io_pressure": 0,
            "memory_pressure": 0,
            "network_bytes_per_second": 0,
            "disk_bytes_per_second": 0,
        }

    def sample(self):
        return copy.deepcopy(self.value)


def test_resource_hysteresis_and_unavailable_fail_closed(settings_factory, monkeypatch):
    service = CaptureService(settings_factory(background_transfer={"enabled": True}))
    work = service.background
    sampler = Sampler()
    work.sampler = sampler
    clock = [100.0]
    monkeypatch.setattr("pipeline.client.resources.time.monotonic", lambda: clock[0])
    with pytest.raises(WorkPaused, match="稳定"):
        work.checkpoint()
    clock[0] += 21
    work.checkpoint()
    sampler.value["memory_available_ratio"] = (
        0.17  # between pause and resume thresholds
    )
    clock[0] += 1
    work.checkpoint()
    sampler.value["io_pressure"] = 30
    clock[0] += 1
    with pytest.raises(WorkPaused, match="I/O"):
        work.checkpoint()
    sampler.value["io_pressure"] = 0
    clock[0] += 1
    with pytest.raises(WorkPaused, match="内存"):
        work.checkpoint()
    sampler.value["memory_available_ratio"] = 0.5
    sampler.value["swapout_pages_per_second"] = 1
    clock[0] += 1
    with pytest.raises(WorkPaused, match="换页"):
        work.checkpoint()


def test_progress_merge_preserves_latest_capture_fields(native):
    service, adapter = native
    service.begin_batch(request())
    episode(adapter)
    service.sync_external()
    row = service.store.all("capture")[0]
    service.update(row["id"], error="new error")
    updated = service.update(row["id"], uploaded_bytes=12)
    assert updated["error"] == "new error"
    assert updated["uploaded_bytes"] == 12


def test_native_manifest_accepted_by_server(native, settings_factory, tmp_path):
    from pipeline.server.ingest import IngestService

    service, adapter = native
    batch = service.begin_batch(request())
    episode(adapter)
    service.sync_external()
    service.end_batch(batch["id"])
    service.seal_pending()
    row = service.store.all("capture")[0]
    manifest = json.loads((Path(row["raw_root"]).parent / "manifest.json").read_text())
    server = settings_factory(
        "server", profiles=service.settings.profiles, default_profile="g1"
    )
    response = IngestService(server).create(manifest, "default", "")
    assert response["manifest_hash"] == digest(manifest)


def test_batch_api_capabilities_auth_and_end(native, monkeypatch):
    from fastapi.testclient import TestClient

    from pipeline.client.app import create_app

    service, adapter = native
    adapter.health = lambda: {"ready": True}
    monkeypatch.setattr("pipeline.client.app.CaptureService", lambda settings: service)
    app = create_app(service.settings, background=False)
    with TestClient(app) as http:
        assert http.post("/api/capture-batches", json=request()).status_code == 401
        http.headers["Authorization"] = f"Bearer {service.settings.token}"
        assert http.get("/api/config").json()["capture_capabilities"][
            "external_trigger"
        ]
        response = http.post("/api/capture-batches", json=request())
        assert response.status_code == 201
        batch = response.json()
        assert http.get("/api/health").json()["batch"]["id"] == batch["id"]
        assert len(http.get("/api/capture-batches").json()) == 1
        assert (
            http.post(f"/api/capture-batches/{batch['id']}/end").json()["state"]
            == "ENDED"
        )
        assert (
            http.post(f"/api/capture-batches/{batch['id']}/end").json()["state"]
            == "ENDED"
        )


def test_missing_final_metadata_never_releases_batch(native):
    service, adapter = native
    batch = service.begin_batch(request())
    packet, _ = episode(adapter)
    service.sync_external()
    packet.with_name("episode.json").unlink()
    assert service.end_batch(batch["id"])["state"] == "RECOVERY_REQUIRED"
    assert service.batch_active.is_set()


def test_live_socket_state_overlays_durable_start_metadata(native):
    service, adapter = native
    service.begin_batch(request())
    episode(adapter, state="recording")
    adapter.status = lambda: {"state": "PAUSED", "episode_index": 0, "frame_count": 123}
    service.sync_external()
    row = service.store.all("capture")[0]
    assert row["state"] == "PAUSED" and row["frame_count"] == 123
    assert not row.get("source_metadata_hash")
