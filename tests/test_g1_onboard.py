"""Native HTTP contracts and managed capture lifecycle without robot processes."""

import copy
import json
import math
import time
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from test_client import request
from test_g1_sonic_provider import write_episode

from pipeline.client.adapters import g1_onboard as module
from pipeline.client.adapters.g1_onboard import G1OnboardAdapter
from pipeline.client.app import create_app
from pipeline.client.service import CaptureService
from pipeline.shared.core import DomainError, atomic_json


class NativeWeb:
    def __init__(self, repo):
        self.repo = repo
        self.state = {
            "enabled": True,
            "phase": "idle",
            "raw_only": True,
            "session": None,
            "error": None,
            "camera": {"ready": True, "depth": False},
        }
        self.calls = []
        self.nonce = None
        self.requests = {}
        self.fail_start_response = False
        self.server = "SonicWebVisualizer/1.0 Python/3.10"
        self.offline = False
        self.cameras = ["ego_view"]

    def respond(self, payload, status=200):
        return httpx.Response(
            status, json=copy.deepcopy(payload), headers={"server": self.server}
        )

    def handle(self, req):
        if self.offline:
            raise httpx.ConnectError("service not started", request=req)
        route = req.url.path
        body = json.loads(req.content) if req.content else {}
        self.calls.append((req.method, route, body))
        if route == "/api/collection/status":
            return self.respond(self.state)
        if route == "/api/status":
            return self.respond(
                {
                    "uptime_seconds": 12.5,
                    "signal_count": 2,
                    "cameras": self.cameras,
                    "camera_age_ms": {name: 12 for name in self.cameras},
                    "camera_rates_hz": {name: 30 for name in self.cameras},
                    "sources": {
                        name: {"online": True}
                        for name in ("camera", "robot", "pico_pose", "manager")
                    }
                }
            )
        if route == "/api/signals":
            assert req.headers["X-Collection-Action"] == "1"
            return self.respond(
                {"signals": ["robot.body_q[0]", "planner.movement[1]"]}
            )
        if route == "/api/history":
            assert req.headers["X-Collection-Action"] == "1"
            selected = req.url.params.get_list("signal")
            current = time.time()
            return self.respond(
                {
                    "now": current,
                    "window_seconds": 20,
                    "series": {
                        signal: [
                            [
                                current - 20 + offset / 4,
                                math.sin(offset / 8 + index) + index,
                            ]
                            for offset in range(80)
                        ]
                        for index, signal in enumerate(selected)
                    },
                }
            )
        assert req.headers["X-Collection-Action"] == "1"
        if route == "/api/collection/camera-depth":
            self.state["camera"]["depth"] = body["depth"]
            return self.respond(self.state["camera"])
        if route == "/api/collection/dataset":
            return self.respond(
                {
                    "exists": False,
                    "episode_count": 0,
                    "next_episode_index": 0,
                    "unfinished_episodes": 0,
                }
            )
        if route == "/api/collection/challenge":
            self.nonce = body["operation"]
            return self.respond({"nonce": self.nonce})
        if route == "/api/collection/start":
            assert body["nonce"] == self.nonce == "start"
            assert self.state["phase"] == "idle"
            output = self.repo / "outputs" / body["subdirectory"] / body["dataset_name"]
            output.mkdir(parents=True)
            self.state.update(
                phase="starting",
                session="sonic-web-example",
                output=str(output),
                task=body["task_prompt"],
                exporter={"state": "idle", "output": str(output)},
            )
            if self.fail_start_response:
                raise httpx.ReadTimeout("response lost", request=req)
            return self.respond(self.state)
        if route == "/api/collection/stop":
            assert body["nonce"] == self.nonce == "stop"
            self.state["phase"] = "stopping"
            return self.respond(self.state)
        if route.startswith("/api/collection/episodes/"):
            action = route.rsplit("/", 1)[-1]
            old = self.requests.get(body["request_id"])
            if old:
                assert old == action
            self.requests[body["request_id"]] = action
            self.state["exporter"]["state"] = (
                "recording" if action == "start" else "idle"
            )
            return self.respond(self.state["exporter"])
        raise AssertionError(route)

    def running(self):
        self.state["phase"] = "running"

    def stopped(self):
        self.state.update(phase="idle", session=None, output=None, exporter=None)


@pytest.fixture
def onboard(settings_factory, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    scripts = repo / "gear_sonic/scripts"
    scripts.mkdir(parents=True)
    (scripts / "run_web_data_visualizer.py").touch()
    visualizer = repo / "gear_sonic/utils/data_collection/web_data_visualizer.html"
    visualizer.parent.mkdir(parents=True)
    visualizer.write_text(
        "<!doctype html><html><head><title>SONIC Onboard Data Visualizer</title>"
        "<style>body{color:#eee}</style></head><body>"
        "<h1>SONIC Onboard Data Visualizer</h1>"
        "<section><h2>采集控制</h2><button>启动服务</button></section>"
        "<script>fetch('/api/collection/status'); fetch('/api/status'); fetch('/api/signals');"
        "const camera = `/api/camera/ego_view.mjpg`;</script>"
        "</body></html>"
    )
    python = tmp_path / "camera-python"
    python.touch()
    profile = json.loads(Path("configs/g1-sonic11-brainco-v1.json").read_text())
    profile["cameras"] = [
        {"id": "egocentric", "source_key": "ego_view", "width": 32, "height": 24}
    ]
    profile["output_fps"] = 10
    settings = settings_factory(
        mode="external",
        capture={
            "adapter": "g1_sonic_onboard",
            "repository": str(repo),
            "python": str(python),
            "service_url": "http://127.0.0.1:8081",
        },
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
    native = NativeWeb(repo)
    original = httpx.Client
    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kw: original(transport=httpx.MockTransport(native.handle), **kw),
    )
    monkeypatch.setattr(G1OnboardAdapter, "_robotics_running", lambda self: True)
    monkeypatch.setattr(
        G1OnboardAdapter, "_check_existing_collectors", lambda self: None
    )
    return settings, native


def batch_request(**kw):
    return request(dataset_name="transfer-box", **kw)


@pytest.mark.asyncio
async def test_preview_closes_native_connection_without_collecting(
    onboard, monkeypatch
):
    settings, native = onboard
    calls = []

    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"--frame\r\nfirst unchanged frame"
            yield b"second unchanged frame"

        async def aclose(self):
            self.closed = True

    body = Body()

    def handle(req):
        calls.append(req.url.path)
        return httpx.Response(
            200,
            stream=body,
            headers={
                "content-type": "multipart/x-mixed-replace; boundary=frame",
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(handle),
            **kw,
        ),
    )
    adapter = G1OnboardAdapter(settings, settings.profile())
    async with adapter.open_preview("egocentric") as source:
        assert await anext(source.chunks) == b"--frame\r\nfirst unchanged frame"
    assert body.closed
    assert calls == ["/api/camera/ego_view.mjpg"]
    assert native.calls == []
    assert adapter.capabilities["preview_stream"] is True
    assert adapter.capabilities["native_client_page"] is True
    assert adapter.capabilities["telemetry"]["max_signals"] == 16


def test_preview_api_auth_validation_and_unchanged_bytes(onboard, monkeypatch):
    settings, native = onboard
    calls = []
    content = (
        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\njpeg\r\n"
    )

    def handle(req):
        calls.append(req.url.path)
        return httpx.Response(
            200,
            content=content,
            headers={
                "content-type": 'multipart/x-mixed-replace; boundary="frame"',
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(handle),
            **kw,
        ),
    )
    with TestClient(create_app(settings, background=False)) as http:
        assert http.get("/assets/preview-stream.js").status_code == 200
        assert http.get("/assets/capture-telemetry.js").status_code == 200
        assert http.get("/api/cameras/unknown/stream").status_code == 404
        response = http.get("/api/cameras/egocentric/stream")
        assert response.status_code == 200
        assert response.content == content
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-accel-buffering"] == "no"
    assert calls == ["/api/camera/ego_view.mjpg"]
    assert native.calls == []


def test_telemetry_api_is_public_on_client_and_bounded(onboard):
    settings, native = onboard
    with TestClient(create_app(settings, background=False)) as http:
        catalog = http.get("/api/capture-telemetry/signals")
        assert catalog.status_code == 200
        assert catalog.json()["signals"] == [
            "robot.body_q[0]",
            "planner.movement[1]",
        ]
        history = http.get(
            "/api/capture-telemetry/history",
            params=[
                ("signal", "robot.body_q[0]"),
                ("signal", "planner.movement[1]"),
            ],
        )
        assert history.status_code == 200
        assert set(history.json()["series"]) == {
            "robot.body_q[0]",
            "planner.movement[1]",
        }
        assert (
            http.get(
                "/api/capture-telemetry/history",
                params=[
                    ("signal", "robot.body_q[0]"),
                    ("signal", "robot.body_q[0]"),
                ],
            ).status_code
            == 400
        )
        assert (
            http.get(
                "/api/capture-telemetry/history",
                params=[
                    ("signal", f"signal[{index}]") for index in range(17)
                ],
            ).status_code
            == 400
        )


def test_native_visualizer_is_the_composed_client_document(
    onboard, monkeypatch
):
    settings, native = onboard
    calls = []
    content = (
        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\njpeg\r\n"
    )

    def handle(req):
        calls.append(req.url.path)
        return httpx.Response(
            200,
            content=content,
            headers={"content-type": "multipart/x-mixed-replace; boundary=frame"},
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(handle), **kw),
    )
    with TestClient(create_app(settings, background=False)) as http:
        assert http.get("/capture-visualizer/").status_code == 200
        assert http.get("/capture-visualizer/api/status").status_code == 200
        index = http.get("/")
        assert index.status_code == 200
        assert "SONIC Onboard Data Visualizer" in index.text
        assert 'id="bp-client"' in index.text
        assert "数据采集与管理" in index.text
        assert "采集控制" in index.text
        assert 'id="native-visualizer-frame"' not in index.text
        assert "'/capture-visualizer/api/status'" in index.text
        assert "'/capture-visualizer/api/signals'" in index.text
        assert "/capture-visualizer/api/camera/" in index.text
        assert "/assets/g1-onboard_collection.js" in index.text
        assert "/assets/g1-onboard_collection.css" in index.text
        assert "/assets/sonic-client.css" in index.text
        assert "/assets/sonic-client.js" in index.text
        collection_js = http.get("/assets/g1-onboard_collection.js")
        assert collection_js.status_code == 200
        assert "/capture-visualizer/api/collection/status" in collection_js.text
        assert http.get("/assets/g1-onboard_collection.css").status_code == 200
        root_policy = index.headers["content-security-policy"]
        assert "script-src 'self' 'unsafe-inline'" in root_policy
        assert "frame-ancestors 'none'" in root_policy

        http.headers["Authorization"] = "Bearer " + settings.token
        page = http.get("/capture-visualizer/")
        assert page.status_code == 200
        assert "SONIC Onboard Data Visualizer" in page.text
        assert "'/capture-visualizer/api/status'" in page.text
        assert "'/capture-visualizer/api/signals'" in page.text
        assert "/capture-visualizer/api/camera/" in page.text
        assert "sonic-visualizer-height" in page.text
        policy = page.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in policy
        assert "script-src 'self' 'unsafe-inline'" in policy

        status = http.get("/capture-visualizer/api/status")
        assert status.status_code == 200
        assert status.json()["cameras"] == ["ego_view"]
        signals = http.get("/capture-visualizer/api/signals")
        assert signals.status_code == 200
        assert signals.json()["signals"] == [
            "robot.body_q[0]",
            "planner.movement[1]",
        ]
        history = http.get(
            "/capture-visualizer/api/history",
            params={"signal": "robot.body_q[0]"},
        )
        assert history.status_code == 200
        assert "robot.body_q[0]" in history.json()["series"]
        stream = http.get("/capture-visualizer/api/camera/ego_view.mjpg")
        assert stream.status_code == 200
        assert stream.content == content
    assert calls == ["/api/camera/ego_view.mjpg"]


def test_native_client_page_rejects_incomplete_captured_html(onboard, monkeypatch):
    settings, _ = onboard
    visualizer = Path(settings.root) / "incomplete-visualizer.html"
    visualizer.parent.mkdir(parents=True, exist_ok=True)
    visualizer.write_text(
        "<!doctype html><html><body><h1>SONIC Onboard Data Visualizer</h1>"
        "<script>fetch('/api/status')</script></body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "NATIVE_CLIENT_HTML", visualizer)
    adapter = G1OnboardAdapter(settings, settings.profile())
    with pytest.raises(DomainError, match="unsupported format"):
        adapter.native_client_html()


def test_native_collection_controls_use_client_batch_contract(onboard):
    settings, native = onboard
    with TestClient(create_app(settings, background=False)) as http:
        http.headers["Authorization"] = "Bearer " + settings.token
        dataset = http.post(
            "/capture-visualizer/api/collection/dataset",
            json={
                "subdirectory": "",
                "dataset_name": "transfer-box",
                "task_prompt": "Fold the cloth",
            },
        )
        assert dataset.status_code == 200
        assert dataset.json()["exists"] is False

        rejected_nonce = http.post(
            "/capture-visualizer/api/collection/challenge",
            json={"operation": "start"},
        ).json()["nonce"]
        rejected = http.post(
            "/capture-visualizer/api/collection/start",
            json={
                "subdirectory": "",
                "dataset_name": "transfer-box",
                "task_prompt": "Fold the cloth",
                "raw_only": False,
                "camera_depth": False,
                "nonce": rejected_nonce,
                "resume_dataset": False,
            },
        )
        assert rejected.status_code == 400
        assert "仅支持保存 Raw" in rejected.json()["detail"]
        assert app_batch_rows(settings) == []

        nonce = http.post(
            "/capture-visualizer/api/collection/challenge",
            json={"operation": "start"},
        ).json()["nonce"]
        started = http.post(
            "/capture-visualizer/api/collection/start",
            json={
                "subdirectory": "",
                "dataset_name": "transfer-box",
                "task_prompt": "Fold the cloth",
                "raw_only": True,
                "camera_depth": False,
                "nonce": nonce,
                "resume_dataset": False,
            },
        )
        assert started.status_code == 200
        assert started.json()["phase"] == "starting"
        batches = app_batch_rows(settings)
        assert len(batches) == 1 and batches[0]["state"] == "ACTIVE"
        assert batches[0]["dataset_name"] == "transfer-box"

        native.running()
        episode = http.post(
            "/capture-visualizer/api/collection/episodes/start",
            json={"request_id": "native-episode-001"},
        )
        assert episode.status_code == 200

        stop_nonce = http.post(
            "/capture-visualizer/api/collection/challenge",
            json={"operation": "stop"},
        ).json()["nonce"]
        stopped = http.post(
            "/capture-visualizer/api/collection/stop",
            json={"nonce": stop_nonce},
        )
        assert stopped.status_code == 200
        assert native.state["phase"] == "stopping"


def app_batch_rows(settings):
    return [
        json.loads(path.read_text())
        for path in (settings.root / "state" / "batch").glob("*.json")
    ]


def test_snapshot_adapter_rejects_telemetry_without_capability(settings_factory):
    settings = settings_factory()
    with TestClient(create_app(settings, background=False)) as http:
        headers = {"Authorization": "Bearer " + settings.token}
        assert (
            http.get(
                "/api/capture-telemetry/signals", headers=headers
            ).status_code
            == 501
        )


@pytest.mark.parametrize(
    "status,content_type",
    [
        (503, "multipart/x-mixed-replace; boundary=frame"),
        (200, "image/jpeg"),
        (200, "multipart/x-mixed-replace"),
    ],
)
def test_preview_rejects_invalid_upstream_before_streaming(
    onboard, monkeypatch, status, content_type
):
    settings, _ = onboard
    original = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    status,
                    content=b"invalid",
                    headers={"content-type": content_type},
                )
            ),
            **kw,
        ),
    )
    with TestClient(create_app(settings, background=False)) as http:
        response = http.get(
            "/api/cameras/egocentric/stream",
            headers={
                "Authorization": "Bearer " + settings.token,
            },
        )
        assert response.status_code == 503


def test_snapshot_adapter_does_not_silently_enable_streaming(settings_factory):
    settings = settings_factory()
    with TestClient(create_app(settings, background=False)) as http:
        headers = {"Authorization": "Bearer " + settings.token}
        assert http.get("/api/cameras/front/stream", headers=headers).status_code == 501
        assert http.get("/api/cameras/front/frame", headers=headers).status_code == 200


def test_full_stack_save_stop_reconcile_and_raw_upload_queue(onboard):
    settings, native = onboard
    service = CaptureService(settings)
    row = service.begin_batch(batch_request(capture_options={"camera_depth": True}))
    assert row["state"] == "ACTIVE"
    assert native.state["camera"]["depth"] is True
    assert service.adapter.status()["state"] == "STARTING"
    body = next(
        body for _, route, body in native.calls if route == "/api/collection/start"
    )
    assert body["dataset_name"] == "transfer-box"
    assert body["task_prompt"] == "Fold the cloth"
    assert body["raw_only"] is True
    native.running()
    service.batch_command(row["id"], "start", "start-request-001")
    assert service.adapter.status()["state"] == "RECORDING"
    failed = service.end_batch(row["id"])
    assert failed["state"] == "RECOVERY_REQUIRED"
    assert not any(route == "/api/collection/stop" for _, route, _ in native.calls)
    packet, metadata = write_episode(
        Path(native.state["output"]) / "episodes/episode_000000"
    )
    metadata.update(episode_index=0, state="saved")
    atomic_json(packet.parent / "episode.json", metadata)
    service.batch_command(row["id"], "stop", "stop-request-001")
    pending = service.end_batch(row["id"])
    assert pending["state"] == "STOPPING"
    assert service.batch_active.is_set()
    service.seal_pending()
    assert service.store.all("capture")[0]["state"] == "SEALING"
    native.stopped()
    service.sync_external()
    assert service.store.get("batch", row["id"])["state"] == "ENDED"
    assert not service.batch_active.is_set()
    assert service.seal_pending()
    capture = service.store.all("capture")[0]
    assert capture["state"] == "SEALED" and capture["upload_state"] == "QUEUED"
    assert Path(capture["upload_sources"]["episode0/packets.raw"]) == packet


def test_restart_and_uncertain_start_never_relaunches_robot(onboard):
    settings, native = onboard
    native.fail_start_response = True
    service = CaptureService(settings)
    row = service.begin_batch(batch_request())
    assert row["state"] == "RECOVERY_REQUIRED"
    assert row["adapter_session"]["output"] == native.state["output"]
    assert service.begin_batch(batch_request())["id"] == row["id"]
    native.running()
    restarted = CaptureService(settings)
    restarted.recover()
    restarted.sync_external()
    assert restarted.current_batch()["state"] == "ACTIVE"
    assert (
        len([route for _, route, _ in native.calls if route == "/api/collection/start"])
        == 1
    )


def test_stopping_survives_restart_and_requires_native_exit_proof(onboard):
    settings, native = onboard
    service = CaptureService(settings)
    row = service.begin_batch(batch_request())
    native.running()
    assert service.end_batch(row["id"])["state"] == "STOPPING"
    restarted = CaptureService(settings)
    restarted.sync_external()
    assert restarted.batch_active.is_set()
    native.stopped()
    restarted.sync_external()
    assert restarted.current_batch() is None


def test_foreign_session_cannot_be_signalled_or_unblock_uploads(onboard):
    settings, native = onboard
    service = CaptureService(settings)
    row = service.begin_batch(batch_request())
    native.running()
    native.state["output"] = "/unrelated/dataset"
    with pytest.raises(DomainError, match="ownership"):
        service.batch_command(row["id"], "discard", "request-0001")
    assert service.end_batch(row["id"], force=True)["state"] == "RECOVERY_REQUIRED"
    assert not any(route == "/api/collection/stop" for _, route, _ in native.calls)
    assert service.batch_active.is_set()


def test_native_error_still_allows_explicit_owned_force_stop(onboard):
    settings, native = onboard
    service = CaptureService(settings)
    row = service.begin_batch(batch_request())
    native.state.update(phase="error", error="exporter exited")
    native.state["exporter"] = {"error": "socket missing"}
    service.adapter.refresh_service()
    assert service.adapter.service_status()["phase"] == "error"
    assert service.end_batch(row["id"], force=True)["state"] == "STOPPING"


@pytest.mark.parametrize(
    "options", [{"raw_only": False}, {"shell": True}, {"camera_depth": "yes"}]
)
def test_untrusted_options_rejected_before_startup(onboard, options):
    settings, _ = onboard
    adapter = G1OnboardAdapter(settings, settings.profile())
    prepare = Mock()
    adapter.prepare_service = prepare
    with pytest.raises(DomainError):
        adapter.begin(
            {
                "dataset_name": "transfer-box",
                "instruction": "task",
                "capture_options": options,
            },
            Mock(),
        )
    prepare.assert_not_called()


def test_unrelated_http_service_is_rejected(onboard):
    settings, native = onboard
    native.server = "OtherService/1"
    adapter = G1OnboardAdapter(settings, settings.profile())
    assert adapter.refresh_service()["phase"] == "unavailable"
    with pytest.raises(DomainError, match="not the SONIC"):
        adapter.prepare_service()


def test_unmanaged_native_session_blocks_background_even_without_client_batch(onboard):
    settings, native = onboard
    native.state.update(phase="running", session="foreign")
    service = CaptureService(settings)
    service.sync_external()
    assert service.background_blocker()
    with pytest.raises(DomainError):
        service.configure_capture_service({"camera_depth": True})


def test_common_http_api_is_public_and_options_episode_commands_validated(onboard):
    settings, native = onboard
    app = create_app(settings, background=False)
    with TestClient(app) as http:
        assert http.post("/api/capture-service/prepare").status_code == 200
        assert http.get("/assets/capture-service.js").status_code == 200
        assert (
            http.patch(
                "/api/capture-service/options",
                json={"options": {"camera_depth": "yes"}},
            ).status_code
            == 422
        )
        assert (
            http.patch(
                "/api/capture-service/options", json={"options": {"unexpected": True}}
            ).status_code
            == 400
        )
        response = http.post("/api/capture-batches", json=batch_request())
        assert response.status_code == 201
        row = response.json()
        native.running()
        route = f"/api/capture-batches/{row['id']}/commands"
        for _ in range(2):
            assert (
                http.post(
                    route, json={"action": "start", "request_id": "same-request-001"}
                ).status_code
                == 200
            )
        assert len(native.requests) == 1
        assert (
            http.post(
                route, json={"action": "shell", "request_id": "shell-request-1"}
            ).status_code
            == 400
        )
        assert (
            http.post(
                f"/api/capture-batches/{row['id']}/end", json={"force": True}
            ).json()["state"]
            == "STOPPING"
        )


def test_prepare_bootstraps_configured_robotics_and_native_web_only(
    onboard, monkeypatch
):
    settings, native = onboard
    script = native.repo / "runService.sh"
    script.touch()
    settings.capture["robotics_service_script"] = str(script)
    native.offline = True
    robotics = {"running": False}
    launches = []
    process = Mock()
    process.poll.return_value = None

    def spawn(args, **kw):
        launches.append(args)
        if args[0] == "/bin/bash":
            robotics["running"] = True
        else:
            native.offline = False
        return process

    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        G1OnboardAdapter, "_robotics_running", lambda self: robotics["running"]
    )
    adapter = G1OnboardAdapter(settings, settings.profile())
    assert adapter.prepare_service()["phase"] == "idle"
    assert launches[0] == ["/bin/bash", str(script)]
    command = launches[1]
    assert command[:2] == [
        settings.capture["python"],
        str(native.repo / "gear_sonic/scripts/run_web_data_visualizer.py"),
    ]
    assert command[command.index("--web-host") + 1] == "127.0.0.1"
    assert (
        command[command.index("--deploy-checkpoint") + 1] == "policy/sonic_v1_1/model"
    )
    assert (
        command[command.index("--deploy-obs-config") + 1]
        == "policy/sonic_v1_1/observation_config.yaml"
    )
    assert command[command.index("--camera-depth") + 1] == "off"
    assert command[command.index("--pose-walk-max-speed") + 1] == "0.3"
    assert "--raw-only" in command and "--pose-walk" in command
    assert not any(route == "/api/collection/start" for _, route, _ in native.calls)


def test_health_and_recovery_never_launch_services(onboard, monkeypatch):
    settings, _ = onboard
    popen = Mock(side_effect=AssertionError("must not start anything"))
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    service = CaptureService(settings)
    service.recover()
    service.sync_external()
    assert service.health()["ready"]
    popen.assert_not_called()


def test_unready_service_rejects_record_command(onboard):
    settings, native = onboard
    service = CaptureService(settings)
    row = service.begin_batch(batch_request())
    with pytest.raises(DomainError, match="ready"):
        service.batch_command(row["id"], "start", "start-request-123")
    assert not native.requests


@pytest.mark.parametrize("name", ["../other", "name;command", "two words", "x" * 65])
def test_dataset_names_never_reach_native_launcher(onboard, name):
    settings, _ = onboard
    adapter = G1OnboardAdapter(settings, settings.profile())
    prepare = Mock()
    adapter.prepare_service = prepare
    with pytest.raises(DomainError):
        adapter.begin({"dataset_name": name, "instruction": "task"}, Mock())
    prepare.assert_not_called()
