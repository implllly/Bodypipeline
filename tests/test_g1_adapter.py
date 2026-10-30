"""Exercise the actual Adapter without launching a recorder or touching hardware."""

import copy
import json
import os
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pipeline.client.adapters import g1_sonic as module
from pipeline.client.adapters.g1_sonic import G1SonicAdapter
from pipeline.shared.core import DomainError, atomic_json


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    script = repo / "gear_sonic/scripts/run_data_exporter.py"
    script.parent.mkdir(parents=True)
    script.touch()
    python = tmp_path / "python"
    python.touch()
    profile = json.loads(Path("configs/g1-sonic11-brainco-v1.json").read_text())
    settings = SimpleNamespace(
        capture={"repository": str(repo), "python": str(python)},
        root=tmp_path / "client",
    )
    result = G1SonicAdapter(settings, profile)
    result.session = {
        "pid": 987654321,
        "process_identity": "123",
        "boot_id": "boot1",
        "socket": str(tmp_path / "control.sock"),
        "output": str(tmp_path / "native"),
    }
    Path(result.session["output"]).mkdir()
    monkeypatch.setattr(module, "boot_identity", lambda: "boot1")
    monkeypatch.setattr(module, "process_identity", lambda pid: "123")
    return result


@pytest.mark.parametrize(
    "identity,boot", [("456", "boot1"), (None, "boot1"), ("123", "boot2")]
)
def test_recycled_missing_or_rebooted_pid_is_never_signalled(
    adapter, monkeypatch, identity, boot
):
    monkeypatch.setattr(module, "process_identity", lambda pid: identity)
    monkeypatch.setattr(module, "boot_identity", lambda: boot)
    kill = Mock(side_effect=AssertionError("must not signal"))
    monkeypatch.setattr(module.os, "kill", kill)
    adapter.end()
    adapter.interrupt()
    assert not kill.called


def test_end_requires_idle_then_pins_process_identity(adapter, monkeypatch):
    status = {"state": "paused"}
    monkeypatch.setattr(adapter, "_request", lambda: status)
    opened, sent, closed = Mock(return_value=42), Mock(), Mock()
    monkeypatch.setattr(os, "pidfd_open", opened, raising=False)
    monkeypatch.setattr(signal, "pidfd_send_signal", sent, raising=False)
    monkeypatch.setattr(os, "close", closed)
    with pytest.raises(DomainError, match="手柄"):
        adapter.end()
    assert not opened.called
    status["state"] = "idle"
    alive = [True]
    monkeypatch.setattr(
        module, "process_identity", lambda pid: "123" if alive[0] else None
    )
    sent.side_effect = lambda *args: alive.__setitem__(0, False)
    adapter.end()
    opened.assert_called_once_with(987654321)
    sent.assert_called_once_with(42, signal.SIGTERM)
    closed.assert_called_once_with(42)


def test_pid_change_after_pidfd_open_does_not_signal(adapter, monkeypatch):
    opened = Mock(return_value=42)
    monkeypatch.setattr(os, "pidfd_open", opened, raising=False)
    monkeypatch.setattr(module, "process_identity", lambda pid: "other")
    sent, closed = Mock(), Mock()
    monkeypatch.setattr(signal, "pidfd_send_signal", sent, raising=False)
    monkeypatch.setattr(os, "close", closed)
    adapter._terminate()
    assert not sent.called
    closed.assert_called_once_with(42)


def test_unreadable_existing_pid_fails_closed(adapter, monkeypatch):
    adapter.session["pid"] = os.getpid()
    # /proc is absent on macOS; emulate only existence, not identity.
    original = Path.exists
    monkeypatch.setattr(
        Path,
        "exists",
        lambda path: True if str(path) == f"/proc/{os.getpid()}" else original(path),
    )
    monkeypatch.setattr(module, "process_identity", lambda pid: None)
    with pytest.raises(DomainError, match="身份读取"):
        adapter.end()


def test_observation_normalizes_states_and_recovers_paths(adapter):
    batch = {"adapter_session": copy.deepcopy(adapter.session)}
    path = Path(adapter.session["output"]) / "episodes/episode_000003/episode.json"
    ep = dict(
        episode_index=3, state="paused", start_time_ns=2_000_000_000, packet_count=9
    )
    atomic_json(path, ep)
    item = adapter.observe()[0]
    assert item["state"] == "PAUSED" and not item["final"]
    assert item["count"] == 9 and item["started_at"] == 2
    adapter.release()
    adapter.recover(batch)
    ep.update(state="saved", end_time_ns=3_000_000_000, bytes=100)
    atomic_json(path, ep)
    item = adapter.observe()[0]
    assert item["state"] == "SEALING" and item["final"]
    path.unlink()
    path.parent.rmdir()
    Path(adapter.session["output"]).rename(path.parents[3] / "moved")
    with pytest.raises(DomainError, match="缺失"):
        adapter.observe()


def test_status_socket_rejects_output_mismatch(adapter, monkeypatch):
    sock = Mock()
    sock.__enter__ = Mock(return_value=sock)
    sock.__exit__ = Mock()
    sock.recv.return_value = b'{"state":"idle","output":"/other"}\n'
    monkeypatch.setattr(module.socket, "socket", Mock(return_value=sock))
    assert "不匹配" in adapter.status()["error"]
    sock.recv.return_value = (
        json.dumps({"state": "paused", "output": adapter.session["output"]}).encode()
        + b"\n"
    )
    assert adapter.status()["state"] == "PAUSED"
    assert json.loads(sock.sendall.call_args.args[0]) == {"action": "status"}


def test_preflight_and_popen_failures_certify_no_launch(adapter, monkeypatch):
    adapter.release()
    adapter.launch_attempted = True  # a previous successful batch
    monkeypatch.setattr(os, "pidfd_open", Mock(), raising=False)
    monkeypatch.setattr(signal, "pidfd_send_signal", Mock(), raising=False)
    monkeypatch.setattr(
        adapter, "_check_existing_collectors", Mock(side_effect=DomainError("已有数采"))
    )
    batch = {
        "id": "newbatch",
        "raw_root": str(adapter.settings.root / "raw"),
        "instruction": "move",
    }
    with pytest.raises(DomainError):
        adapter.begin(batch, Mock())
    assert not adapter.launch_attempted
    monkeypatch.setattr(adapter, "_check_existing_collectors", Mock())
    # Keep the Unix socket below the Linux path bound even with long pytest roots.
    adapter.settings.root = Path("/nonexistent")
    monkeypatch.setattr(Path, "mkdir", Mock())
    monkeypatch.setattr(Path, "open", Mock(return_value=Mock()))
    monkeypatch.setattr(
        module.subprocess, "Popen", Mock(side_effect=OSError("exec failed"))
    )
    with pytest.raises(OSError):
        adapter.begin(batch, Mock())
    assert not adapter.launch_attempted


def test_profile_binding_rejects_other_hands(adapter):
    profile = copy.deepcopy(adapter.profile)
    profile["g1_sonic"]["hand_type"] = "other"
    with pytest.raises(DomainError, match="Profile"):
        G1SonicAdapter(adapter.settings, profile)


def test_begin_persists_ownership_before_socket_and_never_launches_controller(
    adapter, monkeypatch
):
    adapter.release()
    monkeypatch.setattr(os, "pidfd_open", Mock(), raising=False)
    monkeypatch.setattr(signal, "pidfd_send_signal", Mock(), raising=False)
    monkeypatch.setattr(adapter, "_check_existing_collectors", Mock())
    monkeypatch.setattr(os, "fsencode", lambda path: b"short-for-test")
    process = Mock(pid=987654321)
    process.poll.return_value = None
    batch = {
        "id": "newbatch",
        "raw_root": str(adapter.settings.root / "raw"),
        "instruction": "move",
    }

    def popen(args, **kwargs):
        control = Path(args[args.index("--control-socket") + 1])
        control.touch()
        return process

    launch = Mock(side_effect=popen)
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    persisted = []

    def status():
        assert persisted and persisted[0] == adapter.session
        return {"state": "idle", "output": adapter.session["output"]}

    monkeypatch.setattr(adapter, "_request", status)
    result = adapter.begin(batch, lambda session: persisted.append(dict(session)))
    assert result["boot_id"] == "boot1" and result["process_identity"] == "123"
    args = launch.call_args.args[0]
    assert (
        args[1].endswith("gear_sonic/scripts/run_data_exporter.py")
        and "--raw-only" in args
    )
    assert launch.call_args.kwargs["start_new_session"]
    assert launch.call_count == 1
    adapter.close()  # Closing Client must not stop the child.
    process.terminate.assert_not_called()
    process.kill.assert_not_called()


def test_live_child_without_readable_identity_is_not_signalled_or_claimed(
    adapter, monkeypatch
):
    adapter.release()
    monkeypatch.setattr(os, "pidfd_open", Mock(), raising=False)
    monkeypatch.setattr(signal, "pidfd_send_signal", Mock(), raising=False)
    monkeypatch.setattr(adapter, "_check_existing_collectors", Mock())
    monkeypatch.setattr(os, "fsencode", lambda path: b"short-for-test")
    process = Mock(pid=987654321)
    process.poll.return_value = None
    monkeypatch.setattr(module.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(module, "process_identity", lambda pid: None)
    persist = Mock()
    with pytest.raises(DomainError, match="启动失败"):
        adapter.begin(
            {
                "id": "unknown",
                "raw_root": str(adapter.settings.root / "raw"),
                "instruction": "move",
            },
            persist,
        )
    assert adapter.launch_attempted and adapter.session is None
    assert not persist.called
    process.terminate.assert_not_called()


def test_incomplete_ownership_record_never_releases_recorder(adapter):
    adapter.session.pop("boot_id")
    with pytest.raises(DomainError, match="归属记录不完整"):
        adapter.end()
