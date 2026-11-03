import sys
import time

import h5py
import numpy as np
import pytest
from fastapi.testclient import TestClient

from pipeline.client.app import create_app
from pipeline.client.replay import ReplayService
from pipeline.client.service import CaptureService
from pipeline.shared.core import DomainError


@pytest.fixture
def replay_setup(tmp_path, settings_factory):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "episode 1.hdf5"
    with h5py.File(path, "w") as h:
        h["size"] = 3
        for arm in ("masterLeft", "masterRight"):
            h[f"arm/jointStatePosition/{arm}"] = np.full((3, 7), -0.0008)
    settings = settings_factory(
        replay={
            "enabled": True,
            "provider": "agilex_tool",
            "roots": {"data": str(data)},
        }
    )
    capture = CaptureService(settings)
    replay = ReplayService(settings, capture)
    capture.replay = replay
    yield replay, settings, path
    replay.close()


def command(replay, code):
    replay.provider.command = lambda path: [sys.executable, "-u", "-c", code]


def wait(replay):
    deadline = time.monotonic() + 10
    while replay.status()["state"] in {"PREPARING", "RUNNING", "STOPPING"}:
        assert time.monotonic() < deadline, replay.status()
        time.sleep(0.02)
    return replay.status()


def test_reuses_exact_command_and_does_not_apply_joint_limits(replay_setup):
    replay, _, path = replay_setup
    # This is a wrapper, including for the existing tool's negative values.
    assert replay.inspect("data", path.name)["supported"]
    replay.provider.launcher = __import__("pathlib").Path(sys.executable)
    assert replay.provider.command(path) == [
        sys.executable,
        "data_publish.py",
        "--datasetDir",
        str(path),
    ]


def test_completion_stops_spinning_original_tool(replay_setup):
    replay, _, path = replay_setup
    command(
        replay, "import time; [print('frame:', i) for i in range(3)]; time.sleep(60)"
    )
    replay.start("data", path.name, "request-001")
    result = wait(replay)
    assert result["state"] == "COMPLETED"
    assert result["frame"] == 3
    assert replay.process.poll() is not None
    # Retries of a received start must not start another motion.
    assert replay.start("data", path.name, "request-001")["state"] == "COMPLETED"


def test_user_stop_and_shutdown(replay_setup):
    replay, _, path = replay_setup
    command(replay, "import time; print('frame: 0'); time.sleep(60)")
    replay.start("data", path.name, "request-001")
    deadline = time.monotonic() + 5
    while replay.status()["state"] == "PREPARING":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    replay.stop()
    assert wait(replay)["state"] == "STOPPED"
    replay.start("data", path.name, "request-002")
    deadline = time.monotonic() + 5
    while replay.status()["state"] == "PREPARING":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    replay.close()
    assert replay.process.poll() is not None


@pytest.mark.parametrize(
    "code",
    [
        "print('frame: 0')",  # exit zero before completion is still a failed replay
        "print('frame: 2')",  # missing initial frames
        "import time; print('Exception in thread Thread-1'); time.sleep(60)",
    ],
)
def test_partial_or_failed_tool_is_not_success(replay_setup, code):
    replay, _, path = replay_setup
    command(replay, code)
    replay.start("data", path.name, "request-001")
    assert wait(replay)["state"] == "FAILED"


def test_paths_and_new_bodypipeline_package(replay_setup, tmp_path):
    replay, _, path = replay_setup
    assert replay.files("data")["entries"][0]["path"] == path.name
    for invalid in ("../outside.hdf5", str(path)):
        with pytest.raises(DomainError):
            replay.inspect("data", invalid)
    (path.parent / "link.hdf5").symlink_to(path)
    with pytest.raises(DomainError):
        replay.inspect("data", "link.hdf5")
    with h5py.File(path.parent / "raw.hdf5", "w") as h:
        h.create_group("streams")
    info = replay.inspect("data", "raw.hdf5")
    assert not info["supported"]


def test_capture_and_replay_share_one_owner(replay_setup):
    replay, _, path = replay_setup
    command(replay, "import time; time.sleep(60)")
    row = replay.capture.create(
        dict(
            idempotency_key="capture-1",
            instruction="task",
            local_root="default",
            remote_target="default",
        )
    )
    replay.start("data", path.name, "request-001")
    with pytest.raises(DomainError, match="回放"):
        replay.capture.start(row["id"])
    replay.stop()
    wait(replay)
    row["state"] = "RECORDING"
    replay.capture.save(row)
    with pytest.raises(DomainError, match="采集"):
        replay.start("data", path.name, "request-002")


def test_api_auth_and_ui(replay_setup):
    _, settings, path = replay_setup
    app = create_app(settings, background=False)
    command(
        app.state.replay,
        "import time; [print('frame:', i) for i in range(3)]; time.sleep(60)",
    )
    with TestClient(app) as http:
        assert http.get("/replay").status_code == 200
        assert http.get("/assets/client-replay.js").status_code == 200
        redirect = http.get("/replay?capture=record-123", follow_redirects=False)
        assert redirect.status_code == 307
        assert redirect.headers["location"] == "/#replay?capture=record-123"
        assert app.state.replay.status()["state"] == "IDLE"
        assert http.get("/api/replay/status").status_code == 200
        payload = dict(root="data", path=path.name, request_id="request-001")
        response = http.post("/api/replay/start", json=payload)
        assert response.status_code == 202
        assert wait(app.state.replay)["state"] == "COMPLETED"


def test_restart_never_relaunches(replay_setup):
    replay, settings, path = replay_setup
    command(replay, "import time; time.sleep(60)")
    replay.start("data", path.name, "request-001")
    deadline = time.monotonic() + 5
    while replay.status()["state"] == "PREPARING":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    restored = ReplayService(settings, replay.capture)
    assert restored.status()["state"] == "RECOVERY_REQUIRED"
    assert restored.process is None
    with pytest.raises(DomainError):
        restored.start("data", path.name, "request-002")
    if sys.platform == "linux":
        with pytest.raises(DomainError, match="仍在运行"):
            restored.stop()


def test_disabled_by_default(settings_factory):
    settings = settings_factory()
    replay = ReplayService(settings, CaptureService(settings))
    assert not replay.status()["enabled"]
    with pytest.raises(DomainError, match="尚未配置"):
        replay.start("data", "episode.hdf5", "request-001")
