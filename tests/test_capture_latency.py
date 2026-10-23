import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_client import request

from pipeline.client.app import create_app
from pipeline.client.service import CaptureService
from pipeline.shared.core import DomainError


def stopped_capture(service, key="latency-test-001"):
    row = service.create(request(key))
    service.start(row["id"])
    deadline = time.monotonic() + 3
    while not service.adapter.count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service.adapter.count
    return service.stop(row["id"])


def test_slow_sealing_does_not_lock_robot_control(settings_factory, monkeypatch):
    import pipeline.client.packagers as module

    service = CaptureService(settings_factory())
    first = stopped_capture(service)
    entered, release = threading.Event(), threading.Event()
    original = module.file_hash

    def slow_hash(path):
        entered.set()
        assert release.wait(5)
        return original(path)

    monkeypatch.setattr(module, "file_hash", slow_hash)
    assert service.request_keep(first["id"])["state"] == "SEALING"
    with ThreadPoolExecutor(max_workers=2) as pool:
        sealing = pool.submit(service.seal_pending)
        try:
            assert entered.wait(2)
            # This must finish while the first capture's hash is still blocked.
            second = pool.submit(stopped_capture, service, "latency-test-002")
            assert second.result(timeout=2)["state"] == "REVIEW"
            assert service.request_keep(first["id"])["state"] == "SEALING"
            with pytest.raises(DomainError):
                service.stop(first["id"], discard=True)
        finally:
            release.set()
            service.adapter.close()
        assert sealing.result(timeout=3)
    sealed = service.store.get("capture", first["id"])
    assert sealed["state"] == "SEALED"
    assert sealed["upload_state"] == "QUEUED"
    assert sealed["seal_duration_ms"] >= 0
    assert not service.seal_pending()


def test_sealing_recovers_and_corruption_never_uploads(settings_factory):
    settings = settings_factory()
    service = CaptureService(settings)
    first = stopped_capture(service)
    service.request_keep(first["id"])
    restarted = CaptureService(settings)
    restarted.recover()
    assert restarted.store.get("capture", first["id"])["state"] == "SEALING"
    assert restarted.seal_pending()
    assert restarted.store.get("capture", first["id"])["state"] == "SEALED"
    second = stopped_capture(restarted, "latency-test-002")
    restarted.request_keep(second["id"])
    for path in Path(second["raw_root"]).rglob("*.jpg"):
        path.unlink()
    assert restarted.seal_pending()
    invalid = restarted.store.get("capture", second["id"])
    assert invalid["state"] == "REVIEW"
    assert invalid["upload_state"] == "NONE"
    assert invalid["error"]
    # A seal failure must latch the fault: no re-keep, only discard.
    assert invalid["capture_failed"] is True
    with pytest.raises(DomainError):
        restarted.request_keep(second["id"])


def test_http_start_keep_status_and_preview(settings_factory):
    settings = settings_factory()
    app = create_app(settings, background=False)
    with TestClient(app) as http:
        http.post("/api/login", json={"token": settings.token})
        http.headers["Origin"] = "http://testserver"
        response = http.post("/api/captures/start", json=request())
        assert response.status_code == 201
        first = response.json()
        assert first["state"] == "RECORDING"
        assert (
            http.post("/api/captures/start", json=request()).json()["id"] == first["id"]
        )
        status = http.get("/api/capture-status").json()
        assert len(status) == 1
        assert status[0]["state"] == "RECORDING"
        assert "profile" not in status[0]
        assert "idempotency_key" not in status[0]
        assert "raw_root" in status[0]
        assert "profile" in http.get("/api/captures").json()[0]
        full = http.get("/api/cameras/front/frame")
        low = http.get("/api/cameras/front/frame?preview=low")
        assert low.status_code == 200
        assert low.headers["cache-control"] == "no-store"
        assert Image.open(io.BytesIO(full.content)).size == (640, 360)
        assert Image.open(io.BytesIO(low.content)).size == (320, 180)
        assert len(low.content) < len(full.content)
        assert http.get("/api/cameras/front/frame?preview=invalid").status_code == 422
        http.post(f"/api/captures/{first['id']}/stop")
        kept = http.post(f"/api/captures/{first['id']}/keep")
        assert kept.status_code == 202
        assert kept.json()["state"] == "SEALING"
        assert kept.json()["upload_state"] == "NONE"
        assert (
            http.post(f"/api/captures/{first['id']}/keep").json()["state"] == "SEALING"
        )
        second = http.post(
            "/api/captures/start", json=request("latency-test-002")
        ).json()
        assert second["state"] == "RECORDING"
        http.post(f"/api/captures/{second['id']}/stop")
        assert app.state.service.seal_pending()
        assert (
            http.post(f"/api/captures/{first['id']}/keep").json()["state"] == "SEALED"
        )
        assert http.post("/api/captures/start", json=request()).status_code == 409


def test_combined_start_does_not_repeat_unknown_hardware_call(
    settings_factory, monkeypatch
):
    settings = settings_factory()
    app = create_app(settings, background=False)
    calls = []

    def unknown(*args):
        calls.append(args)
        raise TimeoutError("unknown result")

    monkeypatch.setattr(app.state.service.adapter, "start", unknown)
    with TestClient(app) as http:
        http.post("/api/login", json={"token": settings.token})
        http.headers["Origin"] = "http://testserver"
        assert (
            http.post("/api/captures/start", json=request()).json()["state"]
            == "RECOVERY_REQUIRED"
        )
        assert http.post("/api/captures/start", json=request()).status_code == 409
        assert len(calls) == 1


def test_background_worker_seals_only_retained_capture(settings_factory, monkeypatch):
    from pipeline.client.upload import UploadAgent

    monkeypatch.setattr(UploadAgent, "process_once", lambda self: False)
    settings = settings_factory()
    app = create_app(settings)
    with TestClient(app) as http:
        http.post("/api/login", json={"token": settings.token})
        http.headers["Origin"] = "http://testserver"
        row = http.post("/api/captures/start", json=request()).json()
        time.sleep(0.05)
        http.post(f"/api/captures/{row['id']}/stop")
        assert http.get(f"/api/captures/{row['id']}").json()["state"] == "REVIEW"
        assert http.post(f"/api/captures/{row['id']}/keep").status_code == 202
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = http.get(f"/api/captures/{row['id']}").json()
            if result["state"] == "SEALED":
                break
            time.sleep(0.02)
        assert result["state"] == "SEALED"
        assert result["upload_state"] == "QUEUED"
