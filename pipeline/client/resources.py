"""Bounded, cooperative background work; never infer recording state from CPU load."""

from __future__ import annotations

import contextlib
import hashlib
import math
import threading
import time
from pathlib import Path

from pipeline.shared.core import DomainError


class WorkPaused(Exception):
    pass


class LinuxResources:
    def __init__(self, proc=Path("/proc")):
        self.proc = proc
        self.previous = None
        self.previous_io = None

    def sample(self):
        memory = {}
        for line in (self.proc / "meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.strip().split()[0]) * 1024
        if not memory.get("MemTotal") or "MemAvailable" not in memory:
            raise ValueError("MemAvailable unavailable")
        cpu = {}
        for line in (self.proc / "stat").read_text().splitlines():
            parts = line.split()
            if parts and parts[0].startswith("cpu"):
                # Guest time is already included in user/nice.
                values = list(map(int, parts[1:9]))
                cpu[parts[0]] = (sum(values), values[3] + values[4])
        vm = dict(
            line.split() for line in (self.proc / "vmstat").read_text().splitlines()
        )
        stamp = time.monotonic()
        current = (stamp, cpu, int(vm["pswpout"]))
        result = {
            "memory_available_ratio": memory["MemAvailable"] / memory["MemTotal"],
            "memory_available_bytes": memory["MemAvailable"],
            "cpu_percent": None,
            "busiest_core_percent": None,
            "swapout_pages_per_second": None,
        }
        if self.previous:
            previous_time, previous_cpu, previous_swap = self.previous
            usage = {}
            for name, (total, idle) in cpu.items():
                old_total, old_idle = previous_cpu.get(name, (total, idle))
                if total > old_total:
                    usage[name] = 100 * max(
                        0, min(1, 1 - (idle - old_idle) / (total - old_total))
                    )
            result.update(
                cpu_percent=usage.get("cpu"),
                busiest_core_percent=max(
                    (v for k, v in usage.items() if k != "cpu"), default=None
                ),
                swapout_pages_per_second=max(0, current[2] - previous_swap)
                / max(0.001, stamp - previous_time),
            )
        self.previous = current
        network = {}
        for line in (self.proc / "net/dev").read_text().splitlines()[2:]:
            name, counters = line.split(":", 1)
            fields = counters.split()
            if name.strip() != "lo":
                network[name.strip()] = int(fields[0]) + int(fields[8])
        disks = {}
        for line in (self.proc / "diskstats").read_text().splitlines():
            fields = line.split()
            if len(fields) >= 14 and not fields[2].startswith(("loop", "ram")):
                disks[fields[2]] = (int(fields[5]) + int(fields[9])) * 512
        result.update(network_bytes_per_second=None, disk_bytes_per_second=None)
        if self.previous_io:
            old_stamp, old_net, old_disks = self.previous_io
            elapsed = max(0.001, stamp - old_stamp)
            # Max avoids double counting bridge/bond interfaces and disk partitions.
            for kind, values, previous in (
                ("network", network, old_net),
                ("disk", disks, old_disks),
            ):
                result[f"{kind}_bytes_per_second"] = max(
                    (
                        max(0, value - previous.get(name, value)) / elapsed
                        for name, value in values.items()
                    ),
                    default=0,
                )
        self.previous_io = (stamp, network, disks)
        for kind in ("io", "memory"):
            path = self.proc / "pressure" / kind
            # PSI is optional on older kernels. Absence is exposed, not called zero.
            result[f"{kind}_pressure"] = None
            if path.exists():
                for line in path.read_text().splitlines():
                    if line.startswith("some "):
                        result[f"{kind}_pressure"] = float(
                            dict(p.split("=") for p in line.split()[1:])["avg10"]
                        )
        return result


class BackgroundWork:
    def __init__(self, service):
        self.service = service
        cfg = service.settings.background_transfer
        self.enabled = bool(
            cfg.get("enabled", service.capabilities.get("external_trigger", False))
        )
        defaults = {
            "idle_seconds": 20,
            "sample_seconds": 1,
            "memory_pause_ratio": 0.15,
            "memory_resume_ratio": 0.20,
            "cpu_pause_percent": 85,
            "cpu_resume_percent": 70,
            "core_pause_percent": 98,
            "io_pressure_pause": 20,
            "io_pressure_resume": 5,
            "memory_pressure_pause": 5,
            "memory_pressure_resume": 1,
            "rate_bytes_per_second": 5_000_000,
            "chunk_bytes": 1024 * 1024,
            "network_pause_bytes_per_second": 20_000_000,
            "network_resume_bytes_per_second": 10_000_000,
            "disk_pause_bytes_per_second": 100_000_000,
            "disk_resume_bytes_per_second": 50_000_000,
        }
        self.config = {**defaults, **cfg}
        for key in defaults:
            value = self.config[key]
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"Invalid background_transfer.{key}")
        if type(cfg.get("enabled", True)) is not bool:
            raise ValueError("background_transfer.enabled must be boolean")
        if (
            not 0
            < self.config["memory_pause_ratio"]
            < self.config["memory_resume_ratio"]
            < 1
        ):
            raise ValueError("Background memory thresholds must have hysteresis")
        for prefix in ("cpu", "io_pressure", "memory_pressure", "network", "disk"):
            pause = "cpu_pause_percent" if prefix == "cpu" else prefix + "_pause"
            resume = "cpu_resume_percent" if prefix == "cpu" else prefix + "_resume"
            if prefix in {"network", "disk"}:
                pause += "_bytes_per_second"
                resume += "_bytes_per_second"
            if self.config[resume] >= self.config[pause]:
                raise ValueError("Background thresholds must have hysteresis")
        if (
            type(self.config["chunk_bytes"]) is not int
            or not 65536 <= self.config["chunk_bytes"] <= 16 * 1024**2
        ):
            raise ValueError("Background chunk_bytes must be 64 KiB–16 MiB")
        self.sampler = LinuxResources()
        self.lock = threading.Lock()
        self.serial = threading.Lock()
        self.stop = threading.Event()
        self.last_sample = -math.inf
        self.quiet_since = None
        self.ready = False
        self.reason = "等待资源采样"
        self.metrics = {}
        self.hashes = {}

    def checkpoint(self):
        if self.stop.is_set():
            raise WorkPaused("Client 正在关闭")
        reason = (
            self.service.background_blocker()
            if self.enabled or self.service.capabilities.get("external_trigger")
            else ""
        )
        if not self.enabled:
            if reason:
                raise WorkPaused(reason)
            return
        with self.lock:
            now = time.monotonic()
            if reason:
                self.ready, self.quiet_since, self.reason = False, None, reason
                raise WorkPaused(reason)
            if now - self.last_sample >= self.config["sample_seconds"]:
                self.last_sample = now
                try:
                    self.metrics = self.sampler.sample()
                    m, c = self.metrics, self.config
                    if (
                        m["cpu_percent"] is None
                        or m["swapout_pages_per_second"] is None
                    ):
                        raise ValueError("等待 CPU/换页差分采样")
                    suffix = "pause" if self.ready else "resume"
                    reasons = []
                    for kind in ("network", "disk"):
                        value = m[f"{kind}_bytes_per_second"]
                        if value is None:
                            raise ValueError("等待网络/磁盘差分采样")
                        if value > c[f"{kind}_{suffix}_bytes_per_second"]:
                            reasons.append(
                                "网络持续繁忙" if kind == "network" else "磁盘持续繁忙"
                            )
                    if m["memory_available_ratio"] < c[f"memory_{suffix}_ratio"]:
                        reasons.append("可用内存不足")
                    if m["swapout_pages_per_second"] > 0:
                        reasons.append("系统正在换页")
                    if (
                        m["cpu_percent"] > c[f"cpu_{suffix}_percent"]
                        or (m["busiest_core_percent"] or 0) > c["core_pause_percent"]
                    ):
                        reasons.append("CPU 持续繁忙")
                    for kind in ("io", "memory"):
                        pressure = m[f"{kind}_pressure"]
                        if (
                            pressure is not None
                            and pressure > c[f"{kind}_pressure_{suffix}"]
                        ):
                            reasons.append(
                                "磁盘 I/O 压力较高" if kind == "io" else "内存压力较高"
                            )
                    if reasons:
                        self.ready, self.quiet_since, self.reason = (
                            False,
                            None,
                            "；".join(reasons),
                        )
                    else:
                        if self.quiet_since is None:
                            self.quiet_since = now
                        self.ready = now - self.quiet_since >= c["idle_seconds"]
                        self.reason = "" if self.ready else "等待资源稳定空闲"
                except (OSError, ValueError, KeyError, IndexError) as exc:
                    self.ready, self.quiet_since = False, None
                    self.reason = f"资源状态不可用：{exc}"
            if not self.ready:
                raise WorkPaused(self.reason)

    @contextlib.contextmanager
    def lease(self):
        if not self.serial.acquire(blocking=False):
            raise WorkPaused("其他后台任务正在处理")
        try:
            self.checkpoint()
            yield
        finally:
            self.serial.release()

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            self.checkpoint()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.stop.wait(min(0.1, remaining))

    def hash_file(self, path, *, progress=None):
        identity = file_identity(path)
        key = str(path)
        saved = self.hashes.get(key)
        h, offset = (
            (saved[1], saved[2])
            if saved and saved[0] == identity
            else (hashlib.sha256(), 0)
        )
        with path.open("rb") as stream:
            stream.seek(offset)
            while True:
                self.checkpoint()
                if file_identity(path) != identity:
                    self.hashes.pop(key, None)
                    raise DomainError("校验过程中源文件发生变化")
                data = stream.read(self.config["chunk_bytes"])
                if not data:
                    break
                h.update(data)
                offset += len(data)
                # Keep at most one resumable hash to bound memory across queues.
                self.hashes.clear()
                self.hashes[key] = (identity, h, offset)
                if progress:
                    progress(len(data))
                if self.enabled:
                    self.wait(len(data) / self.config["rate_bytes_per_second"])
        self.hashes.pop(key, None)
        return h.hexdigest()

    def status(self):
        try:
            self.checkpoint()
            allowed, reason = True, ""
        except WorkPaused as exc:
            allowed, reason = False, str(exc)
        with self.lock:
            return {
                "enabled": self.enabled,
                "allowed": allowed,
                "reason": reason,
                "metrics": dict(self.metrics),
                "config": dict(self.config),
            }


def file_identity(path):
    path = Path(path).absolute()
    if path.resolve() != path or not path.is_file():
        raise DomainError(f"文件不是受管普通文件：{path}")
    s = path.stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]
