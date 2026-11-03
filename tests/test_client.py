import json
import time

import pytest
from fastapi.testclient import TestClient

from pipeline.client.app import create_app
from pipeline.client.service import CaptureService
from pipeline.shared.core import DomainError, digest


def request(key="test-key-001", **changes):
    return dict(
        instruction="Fold the cloth",
        idempotency_key=key,
        local_root="default",
        local_prefix="",
        remote_target="default",
        remote_prefix="",
        **changes,
    )


def test_mock_capture_lifecycle(settings_factory):
    s = settings_factory()
    service = CaptureService(s)
    row = service.create(request())
    assert service.create(request())["id"] == row["id"]
    assert service.start(row["id"])["state"] == "RECORDING"
    assert service.start(row["id"])["state"] == "RECORDING"
    time.sleep(0.2)
    stopped = service.stop(row["id"])
    assert stopped["state"] == "REVIEW"
    assert service.keep(row["id"])["state"] == "SEALED"
    sealed = service.store.get("capture", row["id"])
    assert sealed["upload_state"] == "QUEUED"
    from pathlib import Path

    manifest = json.loads(
        (Path(sealed["raw_root"]).parent / "manifest.json").read_text()
    )
    assert digest(manifest) == sealed["manifest_hash"]
    assert manifest["raw_format"] == "agilex_joint_camera_hdf5_v1"
    assert [f["path"] for f in manifest["files"]] == ["episode.hdf5"]
    with pytest.raises(DomainError):
        service.stop(row["id"], discard=True)


def test_discard_never_queues(settings_factory):
    service = CaptureService(settings_factory())
    row = service.create(request())
    service.start(row["id"])
    time.sleep(0.1)
    dropped = service.stop(row["id"], discard=True)
    assert dropped["state"] == "DISCARDED"
    assert dropped["upload_state"] == "NONE"
    with pytest.raises(DomainError):
        service.keep(row["id"])


def test_unknown_start_is_not_retried(settings_factory):
    class Adapter:
        starts = 0

        def health(self):
            return {"ready": True}

        def start(self, *args):
            self.starts += 1
            raise TimeoutError("unknown result")

    adapter = Adapter()
    service = CaptureService(settings_factory(), adapter)
    row = service.create(request())
    assert service.start(row["id"])["state"] == "RECOVERY_REQUIRED"
    with pytest.raises(DomainError):
        service.start(row["id"])
    assert adapter.starts == 1
    second = service.create(request("test-key-002"))
    with pytest.raises(DomainError):
        service.start(second["id"])


def test_path_and_idempotency_guards(settings_factory):
    service = CaptureService(settings_factory())
    service.create(request())
    changed = request()
    changed["instruction"] = "different"
    with pytest.raises(DomainError):
        service.create(changed)
    for value in ["../outside", "/absolute", "foo;rm", "foo//bar", r"foo\bar"]:
        invalid = request("invalid-key")
        invalid["local_prefix"] = value
        with pytest.raises(DomainError):
            service.create(invalid)


def test_client_web_has_no_key_gate(settings_factory):
    s = settings_factory()
    with TestClient(create_app(s, background=False)) as http:
        assert http.get("/").status_code == 200
        assert http.get("/api/config").status_code == 200
        assert (
            http.get(
                "/api/config", headers={"Authorization": "Bearer " + s.device_token}
            ).status_code
            == 200
        )
        assert http.post("/api/login", json={"token": s.token}).status_code == 404
        assert http.post("/api/captures", json=request()).status_code == 201
        assert http.get("/api/cameras/not-a-camera/frame").status_code == 404
        assert (
            http.get("/api/cameras/front/frame").headers["content-type"] == "image/jpeg"
        )


def test_health_fault_cannot_be_sealed_after_stop(settings_factory):
    service = CaptureService(settings_factory())
    row = service.create(request())
    service.start(row["id"])
    time.sleep(0.1)
    stopped = service.stop(row["id"], fault="disk low-water mark")
    assert stopped["state"] == "REVIEW"
    assert stopped["capture_failed"]
    assert stopped["error"] == "disk low-water mark"
    with pytest.raises(DomainError):
        service.keep(row["id"])
    # A subsequent ordinary stop does not erase the latched failure.
    assert service.stop(row["id"])["capture_failed"]


def test_recover_latches_legacy_seal_failure(settings_factory):
    settings = settings_factory()
    service = CaptureService(settings)
    legacy = service.create(request())
    # Shape written by older builds: seal ran and failed, no fault flag set.
    legacy.update(
        state="REVIEW",
        error="采集流为空",
        seal_requested_at=time.time(),
        seal_started_at=time.time(),
        seal_duration_ms=12.0,
    )
    service.save(legacy)
    recorder_error = service.create(request("test-key-002"))
    recorder_error.update(state="REVIEW", error="recorder 报告错误")
    service.save(recorder_error)

    restarted = CaptureService(settings)
    restarted.recover()
    failed = restarted.store.get("capture", legacy["id"])
    assert failed["capture_failed"] is True
    assert failed["seal_failed"] is True
    assert not restarted.store.get("capture", recorder_error["id"]).get(
        "capture_failed"
    )
    with pytest.raises(DomainError):
        restarted.request_keep(legacy["id"])
    # Re-running recovery is idempotent and keeps the original error text.
    restarted.recover()
    again = restarted.store.get("capture", legacy["id"])
    assert again["seal_failed"] is True and again["error"] == "采集流为空"
