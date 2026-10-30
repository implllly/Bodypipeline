"""Own only a GR00T raw subscriber, never deploy/PICO/camera control processes."""

from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from pipeline.shared.core import DomainError, confined, digest

from ..replay import process_identity


def boot_identity():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


STATES = {
    "idle": "IDLE",
    "recording": "RECORDING",
    "paused": "PAUSED",
    "need_to_save": "SAVING",
    "saved": "SEALING",
    "discarded": "DISCARDED",
    "interrupted": "REVIEW",
}


def episode_observation(ep):
    state = STATES.get(ep.get("state"))
    if state is None or state == "IDLE":
        raise DomainError("未知原始 episode 状态")
    return {
        "index": ep["episode_index"],
        "state": state,
        "final": ep["state"] in {"saved", "discarded", "interrupted"},
        "started_at": ep.get("start_time_ns", 0) / 1e9,
        "stopped_at": ep.get("end_time_ns", 0) / 1e9,
        "count": ep.get("packet_count", 0),
        "count_unit": "packets",
        "size_bytes": ep.get("bytes", 0),
        "metadata_hash": digest(ep),
        "error": ep.get("error"),
    }


class G1SonicAdapter:
    capabilities = {
        "external_trigger": True,
        "raw_format": "g1_sonic_raw_v1",
        "packager": "g1_sonic_raw",
        "preview_passthrough": True,
    }

    def __init__(self, settings, profile):
        if (
            profile.get("robot_type") != "g1_brainco"
            or profile.get("g1_sonic", {}).get("sonic_version") != "1.1"
            or profile.get("g1_sonic", {}).get("hand_type") != "brainco"
        ):
            raise DomainError("G1 Adapter 仅绑定 SONIC 1.1 / BrainCo Profile")
        self.settings, self.profile = settings, profile
        self.config = settings.capture
        self.repo = Path(self.config.get("repository", "")).expanduser().absolute()
        self.python = Path(self.config.get("python", "")).expanduser().absolute()
        if (
            self.repo.resolve() != self.repo
            or not self.repo.is_dir()
            or not self.python.is_file()
        ):
            raise DomainError("G1 采集程序 repository/python 路径无效")
        self.session = None
        self.process = None
        self.launch_attempted = False
        self.observations = {}
        self.preview_lock = threading.Lock()
        self.preview_frames = {}
        self.preview_thread = None
        self.preview_stop = threading.Event()
        self.preview_requested = 0

    def recover(self, batch):
        self.session = batch.get("adapter_session")

    def _alive(self):
        s = self.session
        if not s:
            return False
        if (
            type(s.get("pid")) is not int
            or s["pid"] <= 0
            or not s.get("boot_id")
            or not s.get("process_identity")
        ):
            raise DomainError("采集器归属记录不完整，拒绝推测已经退出")
        if (
            self.process
            and self.process.pid == s["pid"]
            and self.process.poll() is not None
        ):
            return False
        if s.get("boot_id") != boot_identity():
            return False
        current = process_identity(s["pid"])
        if current is None and Path(f"/proc/{s['pid']}").exists():
            raise DomainError("进程身份读取失败，拒绝推测采集器已经退出")
        return current is not None and current == s["process_identity"]

    def _request(self, action="status"):
        if not self._alive():
            raise DomainError("原始采集器未运行或进程身份无法核实")
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(1)
            sock.connect(self.session["socket"])
            sock.sendall(json.dumps({"action": action}).encode() + b"\n")
            response = b""
            while not response.endswith(b"\n"):
                part = sock.recv(4096)
                if not part or len(response) + len(part) > 65536:
                    raise DomainError("采集器状态响应无效")
                response += part
        result = json.loads(response)
        if result.get("output") != self.session["output"]:
            raise DomainError("采集器输出目录与受管作业不匹配")
        return result

    def health(self):
        status = self.status()
        return {
            "ready": not status.get("error"),
            "status": status,
            "detail": "仅管理 raw 订阅采集器，不启动机器人控制器",
        }

    def status(self):
        if not self.session:
            return {"state": "IDLE", "managed": False}
        try:
            result = self._request()
            state = STATES.get(result.get("state"))
            if state is None:
                raise DomainError("未知采集器状态")
            return {
                **result,
                "native_state": result["state"],
                "state": state,
                "managed": True,
            }
        except (OSError, ValueError, KeyError, DomainError) as exc:
            return {"state": "unknown", "managed": True, "error": str(exc)}

    def begin(self, batch, persist):
        if self.session or self._alive():
            raise DomainError("已有采集器或待恢复作业")
        self.launch_attempted = False
        boot = boot_identity()  # Fail before launch if Linux ownership is unavailable.
        root = Path(batch["raw_root"])
        output = root / "native"
        script = self.repo / "gear_sonic/scripts/run_data_exporter.py"
        if not script.is_file() or output.exists():
            raise DomainError("采集入口缺失或输出目录已存在")
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise DomainError("G1 采集需要支持 pidfd 的 Linux/Python 运行环境")
        self._check_existing_collectors()
        root.mkdir(parents=True, exist_ok=True)
        control = self.settings.root / "recorder-sockets" / (batch["id"][:16] + ".sock")
        control.parent.mkdir(parents=True, exist_ok=True)
        if len(os.fsencode(control)) > 100 or control.exists():
            raise DomainError("Unix Socket 路径过长或已存在，请配置较短的 Client root")
        args = [
            str(self.python),
            str(script),
            "--raw-only",
            "--dataset-name",
            "native",
            "--root-output-dir",
            str(root),
            "--task-prompt",
            batch["instruction"],
            "--control-socket",
            str(control),
            "--record-wrist-cameras",
            "--raw-buffer-mb",
            str(self.config.get("raw_buffer_mb", 64)),
        ]
        if not self.config.get("text_to_speech", True):
            args.append("--no-text-to-speech")
        # Paths and launch flags are server-owned configuration, not browser input.
        for flag, default in [
            ("camera", 5555),
            ("sonic-zmq", 5556),
            ("state-zmq", 5557),
        ]:
            key = flag.replace("-", "_")
            args += [
                f"--{flag}-host",
                str(self.config.get(key + "_host", "127.0.0.1")),
                f"--{flag}-port",
                str(self.config.get(key + "_port", default)),
            ]
        log = (root / "recorder.log").open("ab")
        try:
            self.launch_attempted = True
            self.process = subprocess.Popen(
                args,
                cwd=self.repo,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        except OSError:
            self.launch_attempted = False
            raise
        finally:
            log.close()
        identity = process_identity(self.process.pid)
        if identity is None:
            if self.process.poll() is not None:
                self.launch_attempted = False
            raise DomainError("采集进程启动失败，请检查 recorder.log")
        self.session = {
            "pid": self.process.pid,
            "process_identity": identity,
            "socket": str(control),
            "output": str(output),
            "boot_id": boot,
        }
        persist(self.session)  # Persist ownership before waiting for socket readiness.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not self._alive():
                raise DomainError("采集器退出，请检查 recorder.log")
            if control.exists():
                self._request()
                return self.session
            time.sleep(0.1)
        raise DomainError("采集器启动结果未知，请检查进程和 recorder.log，不自动重试")

    def _check_existing_collectors(self):
        for directory in Path("/proc").iterdir():
            if directory.name.isdigit():
                try:
                    command = (directory / "cmdline").read_bytes()
                except FileNotFoundError:
                    continue
                if (
                    b"run_data_exporter.py" in command
                    or b"gear_sonic.scripts.run_data_exporter" in command
                ):
                    raise DomainError(
                        "检测到已有数采程序，拒绝启动第二个采集器；请先结束旧采集会话"
                    )

    def interrupt(self):
        if self._alive():
            # SIGTERM marks any active raw episode interrupted, never saved.
            self._terminate()

    def _terminate(self):
        # A pidfd pins the identity across the check/signal race. Never fall back
        # to kill(pid), which could signal an unrelated recycled PID.
        fd = os.pidfd_open(self.session["pid"])
        try:
            if not self._alive():
                return
            signal.pidfd_send_signal(fd, signal.SIGTERM)
        finally:
            os.close(fd)

    def end(self):
        if not self.session:
            raise DomainError("没有受管采集器")
        if self._alive():
            status = self._request()
            if status.get("state") != "idle":
                raise DomainError("请先用手柄结束当前录制并等待保存完成")
            # Only this raw subscriber is signalled, never its process group or robot.
            self._terminate()
            deadline = time.monotonic() + 10
            while self._alive() and time.monotonic() < deadline:
                if self.process:
                    self.process.poll()
                time.sleep(0.05)
            if self._alive():
                raise DomainError("采集器尚未退出，后台上传仍保持暂停")

    def release(self):
        self.session = None
        self.observations.clear()

    def observe(self):
        if not self.session:
            return []
        root = Path(self.session["output"])
        if root.resolve() != root or not root.is_dir():
            raise DomainError("原始采集目录缺失或包含符号链接")
        result = []
        # episode manifests are the durable event ledger; scanning catches clicks
        # faster than status polling and works after Client/log rotation/restarts.
        for path in sorted(root.glob("episodes/episode_*/episode.json")):
            if path.resolve() != path:
                raise DomainError("episode 元数据包含符号链接")
            stat = path.stat()
            signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            cached = self.observations.get(str(path))
            ep = (
                cached[1]
                if cached and cached[0] == signature
                else json.loads(path.read_text())
            )
            self.observations[str(path)] = (signature, ep)
            if (
                type(ep.get("episode_index")) is not int
                or path.parent.name != f"episode_{ep['episode_index']:06d}"
            ):
                raise DomainError("episode 编号与目录不一致")
            config = root
            capture = ep.get("capture_directory", ".")
            if capture != ".":
                config = confined(root, capture)
            result.append(
                {
                    "key": digest({"output": str(root), "index": ep["episode_index"]}),
                    **episode_observation(ep),
                    "metadata_path": str(path),
                    "source_directory": str(path.parent),
                    "robot_config_path": str(config / "robot_config.json"),
                }
            )
        return result

    def start(self, *args):
        raise DomainError("此 Adapter 使用外部按钮启动录制，请进入采集作业后使用手柄")

    def stop(self, *args):
        raise DomainError("请用手柄结束或丢弃录制；结束采集作业不会控制机器人")

    def frame(self, camera_id):
        # A trusted optional visualizer URL can be reused; otherwise subscribe
        # lazily to camera ZMQ and retain only bounded encoded RGB frames.
        origin = self.config.get("preview_url")
        camera = next(
            (c for c in self.profile["cameras"] if c["id"] == camera_id), None
        )
        if not camera:
            return None
        if not origin:
            if not self.config.get("preview_zmq", False):
                return None
            self.preview_requested = time.monotonic()
            with self.preview_lock:
                if self.preview_thread is None or not self.preview_thread.is_alive():
                    self.preview_thread = threading.Thread(
                        target=self._preview, daemon=True
                    )
                    self.preview_thread.start()
                item = self.preview_frames.get(camera["source_key"])
            return item[1] if item and time.monotonic() - item[0] < 2 else None
        import httpx

        try:
            with httpx.Client(timeout=1, trust_env=False) as http:
                with http.stream(
                    "GET", f"{origin}/api/camera/{camera['source_key']}.mjpg"
                ) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for part in response.iter_bytes(16384):
                        data.extend(part)
                        start, end = data.find(b"\xff\xd8"), data.find(b"\xff\xd9")
                        if start >= 0 and end > start:
                            return bytes(data[start : end + 2])
                        if len(data) > 2 * 1024**2:
                            return None
        except httpx.HTTPError:
            return None

    def _preview(self):
        import msgpack
        import zmq

        context = zmq.Context()
        sub = context.socket(zmq.SUB)
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        sub.setsockopt(zmq.RCVHWM, 6)
        sub.setsockopt(zmq.MAXMSGSIZE, 16 * 1024**2)
        sub.setsockopt(zmq.LINGER, 0)
        sub.connect(
            f"tcp://{self.config.get('camera_host', '127.0.0.1')}:{self.config.get('camera_port', 5555)}"
        )
        wanted = {c["source_key"] for c in self.profile["cameras"]}
        last = {}
        try:
            while (
                not self.preview_stop.is_set()
                and time.monotonic() - self.preview_requested < 5
            ):
                if not sub.poll(200):
                    continue
                frame = sub.recv()
                if len(frame) > 16 * 1024**2:
                    continue
                try:
                    payload = msgpack.unpackb(frame, raw=False)
                    images = payload.get("images", {})
                    for name in wanted:
                        image = images.get(name)
                        now = time.monotonic()
                        if image is None or now - last.get(name, 0) < 0.3:
                            continue
                        if isinstance(image, str):
                            image = base64.b64decode(image, validate=True)
                        if (
                            not isinstance(image, bytes)
                            or len(image) > 2 * 1024**2
                            or not image.startswith(b"\xff\xd8")
                        ):
                            continue
                        with self.preview_lock:
                            self.preview_frames[name] = (now, image)
                        last[name] = now
                except (ValueError, TypeError, AttributeError):
                    continue
        finally:
            sub.close()
            context.term()

    def close(self):
        # Client shutdown never implicitly stops a recording or robot process.
        self.preview_stop.set()
        if self.preview_thread:
            self.preview_thread.join(timeout=2)
