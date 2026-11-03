"""Exercise the actual Adapter without launching a recorder or touching hardware."""

import copy
import json
import os
import signal
import socket
import struct
import tempfile
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


@pytest.fixture
def attachable(adapter, tmp_path, monkeypatch):
    session = dict(adapter.session)
    adapter.release()
    with tempfile.TemporaryDirectory(prefix="g1-attach-", dir="/tmp") as directory:
        control = Path(directory).resolve() / "control.sock"
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(control))
            session["socket"] = str(control)
            adapter.config["attach_existing"] = session
            proc_root = tmp_path / "proc"
            proc = proc_root / str(session["pid"])
            proc.mkdir(parents=True)
            (proc / "cwd").symlink_to(adapter.repo)
            (proc / "exe").symlink_to(adapter.python)
            command = [
                str(adapter.python),
                "gear_sonic/scripts/run_data_exporter.py",
                "--raw-only",
                "--record-wrist-cameras",
                "--root-output-dir",
                str(Path(session["output"]).parent),
                "--dataset-name",
                Path(session["output"]).name,
                "--control-socket",
                session["socket"],
            ]
            (proc / "cmdline").write_bytes("\0".join(command).encode() + b"\0")
            atomic_json(
                Path(session["output"]) / "robot_config.json",
                {
                    "hand_type": "brainco",
                    "hand_num_motors": 6,
                    "hand_units": "normalized",
                    "model_path": "policy/sonic_v1_1/model_decoder.onnx",
                },
            )
            monkeypatch.setattr(
                module,
                "Path",
                lambda *args: proc_root if args == ("/proc",) else Path(*args),
            )
            monkeypatch.setattr(
                adapter,
                "_request",
                lambda: {"state": "recording", "output_format": "raw"},
            )
            yield adapter, session, proc, command


def test_attach_live_recording_persists_identity_without_start_or_stop(
    attachable, monkeypatch
):
    adapter, session, _, _ = attachable
    launch = Mock(side_effect=AssertionError("must reuse existing recorder"))
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    monkeypatch.setattr(
        adapter, "_terminate", Mock(side_effect=AssertionError("must not stop"))
    )
    persist = Mock()
    root = adapter.settings.root / "raw"
    result = adapter.begin({"raw_root": str(root)}, persist)
    assert root.is_dir()
    assert result == {**session, "attached": True}
    persist.assert_called_once_with(result)
    assert adapter.launch_attempted and not launch.called
    adapter.close()
    adapter.release()
    adapter.recover({"adapter_session": result})
    assert adapter.session == result and adapter._alive()


@pytest.mark.parametrize(
    "change", ["boot", "identity", "raw", "socket", "python", "profile", "state"]
)
def test_attach_rejects_mismatched_process_format_and_profile(
    attachable, monkeypatch, change
):
    adapter, session, proc, command = attachable
    if change == "boot":
        session["boot_id"] = "old-boot"
    elif change == "identity":
        session["process_identity"] = "old-process"
    elif change == "raw":
        command.remove("--raw-only")
    elif change == "socket":
        command[-1] = "/other.sock"
    elif change == "python":
        (proc / "exe").unlink()
        (proc / "exe").symlink_to(adapter.repo)
    elif change == "profile":
        atomic_json(
            Path(session["output"]) / "robot_config.json", {"hand_type": "other"}
        )
    else:
        monkeypatch.setattr(
            adapter, "_request", lambda: {"state": "unknown", "output_format": "raw"}
        )
    (proc / "cmdline").write_bytes("\0".join(command).encode() + b"\0")
    persist = Mock()
    with pytest.raises(DomainError):
        adapter.begin({}, persist)
    assert (
        not persist.called and adapter.session is None and not adapter.launch_attempted
    )


def test_attach_does_not_ignore_another_collector(attachable):
    adapter, _, proc, _ = attachable
    second = proc.parent / "123456"
    second.mkdir()
    (second / "cmdline").write_bytes(b"python\0run_data_exporter.py\0")
    with pytest.raises(DomainError, match="第二个"):
        adapter.begin({}, Mock())
    assert adapter.session is None


def test_failed_attachment_persistence_keeps_recoverable_identity(attachable):
    adapter, session, _, _ = attachable
    with pytest.raises(OSError, match="disk"):
        adapter.begin(
            {"raw_root": str(adapter.settings.root / "raw")},
            Mock(side_effect=OSError("disk")),
        )
    assert adapter.session == {**session, "attached": True}
    assert adapter.launch_attempted


def test_attached_socket_must_belong_to_the_pinned_recorder(adapter, monkeypatch):
    adapter.session["attached"] = True
    sock = Mock()
    sock.__enter__ = Mock(return_value=sock)
    sock.__exit__ = Mock(return_value=False)
    sock.getsockopt.return_value = struct.pack("3i", 1, os.getuid(), os.getgid())
    monkeypatch.setattr(module.socket, "SO_PEERCRED", 17, raising=False)
    monkeypatch.setattr(module.socket, "socket", Mock(return_value=sock))
    with pytest.raises(DomainError, match="Socket.*身份"):
        adapter._request()
    sock.sendall.assert_not_called()
    sock.getsockopt.return_value = struct.pack(
        "3i", adapter.session["pid"], os.getuid(), os.getgid()
    )
    sock.recv.return_value = (
        json.dumps({"state": "recording", "output": adapter.session["output"]}).encode()
        + b"\n"
    )
    assert adapter._request()["state"] == "recording"
