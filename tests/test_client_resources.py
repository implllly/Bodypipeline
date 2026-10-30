import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_client import request
from test_g1_client import Sampler
from test_g1_client import native as native_fixture

from pipeline.client.replay import ReplayService
from pipeline.client.resources import LinuxResources, WorkPaused
from pipeline.client.service import CaptureService
from pipeline.shared.core import DomainError

native = native_fixture


def proc_files(root, *, user=100, idle=900, swap=0, net=100, disk=100):
    root.mkdir(exist_ok=True)
    (root / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 400 kB\n")
    (root / "stat").write_text(
        f"cpu {user} 0 0 {idle} 0 0 0 0 30 0\ncpu0 {user} 0 0 {idle} 0 0 0 0\n"
    )
    (root / "vmstat").write_text(f"pswpout {swap}\n")
    (root / "net").mkdir(exist_ok=True)
    (root / "net/dev").write_text(
        f"headers\nheaders\n eth0: {net} 0 0 0 0 0 0 0 {net} 0 0 0 0 0 0 0\n lo: 999999 0 0 0 0 0 0 0 999999 0 0 0 0 0 0 0\n"
    )
    (root / "diskstats").write_text(f"8 0 sda 0 0 {disk} 0 0 0 {disk} 0 0 0 0\n")


def test_linux_differential_metrics_and_optional_psi(tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("pipeline.client.resources.time.monotonic", lambda: clock[0])
    proc_files(tmp_path)
    sampler = LinuxResources(tmp_path)
    first = sampler.sample()
    assert first["cpu_percent"] is None and first["io_pressure"] is None
    assert first["memory_available_bytes"] == 409600
    clock[0] += 2
    proc_files(tmp_path, user=140, idle=960, swap=6, net=2100, disk=200)
    (tmp_path / "pressure").mkdir()
    for name, value in (("io", 12.5), ("memory", 0.5)):
        (tmp_path / "pressure" / name).write_text(
            f"some avg10={value} avg60=0 avg300=0 total=100\n"
        )
    result = sampler.sample()
    assert result["cpu_percent"] == pytest.approx(40)
    assert result["swapout_pages_per_second"] == 3
    assert result["network_bytes_per_second"] == 2000
    assert result["disk_bytes_per_second"] == 51200
    assert result["io_pressure"] == 12.5


@pytest.mark.parametrize("kind", ["network", "disk"])
def test_io_throughput_hysteresis_and_sampler_failure(
    settings_factory, monkeypatch, kind
):
    work = CaptureService(
        settings_factory(background_transfer={"enabled": True})
    ).background
    work.sampler = sampler = Sampler()
    clock = [100.0]
    monkeypatch.setattr("pipeline.client.resources.time.monotonic", lambda: clock[0])
    with pytest.raises(WorkPaused):
        work.checkpoint()
    clock[0] += 21
    work.checkpoint()
    sampler.value[f"{kind}_bytes_per_second"] = (
        work.config[f"{kind}_pause_bytes_per_second"] + 1
    )
    clock[0] += 2
    with pytest.raises(WorkPaused, match="繁忙"):
        work.checkpoint()
    sampler.sample = lambda: (_ for _ in ()).throw(OSError("unavailable"))
    clock[0] += 2
    with pytest.raises(WorkPaused, match="不可用"):
        work.checkpoint()


def test_batch_revokes_and_drains_existing_work_before_launch(native):
    service, adapter = native
    entered, release, launched = threading.Event(), threading.Event(), threading.Event()
    begin = adapter.begin

    def launch(*args):
        launched.set()
        return begin(*args)

    adapter.begin = launch

    def worker():
        with service.background.lease():
            entered.set()
            assert release.wait(5)
            with pytest.raises(WorkPaused):
                service.background.checkpoint()

    with ThreadPoolExecutor(2) as pool:
        running = pool.submit(worker)
        assert entered.wait(2)
        starting = pool.submit(service.begin_batch, request())
        assert service.batch_active.wait(2)
        assert not launched.is_set()
        release.set()
        running.result(5)
        assert starting.result(5)["state"] == "ACTIVE"
        assert launched.is_set()


def test_replay_start_and_batch_share_lifecycle_lock(native):
    service, _ = native
    replay = ReplayService(service.settings, service)
    service.replay = replay
    entered, release = threading.Event(), threading.Event()

    def prepare(*args):
        entered.set()
        assert release.wait(5)
        replay.row["state"] = "PREPARING"

    replay._start = prepare
    with ThreadPoolExecutor(2) as pool:
        playing = pool.submit(replay.start, "root", "file", "request-1")
        assert entered.wait(2)
        starting = pool.submit(service.begin_batch, request())
        release.set()
        playing.result(5)
        with pytest.raises(DomainError, match="回放"):
            starting.result(5)
    with pytest.raises(WorkPaused, match="回放"):
        service.background.checkpoint()
