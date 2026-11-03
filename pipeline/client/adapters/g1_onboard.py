"""Client ownership and upload integration for the native SONIC supervisor."""

from __future__ import annotations

import re
import subprocess
import time
from contextlib import asynccontextmanager
from email.message import Message
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import httpx

from pipeline.shared.core import DomainError

from .base import PreviewStream
from .g1_sonic import G1SonicAdapter

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
NATIVE_CLIENT_HTML = (
    Path(__file__).resolve().parents[2] / "web" / "g1-sonic-8081.html"
)


class G1OnboardAdapter(G1SonicAdapter):
    def __init__(self, settings, profile):
        super().__init__(settings, profile)
        self.config = dict(settings.capture)
        self.origin = self.config.get("service_url", "http://127.0.0.1:8081")
        origin = urlsplit(self.origin)
        if (
            origin.scheme != "http"
            or origin.hostname != "127.0.0.1"
            or not origin.port
            or origin.path not in {"", "/"}
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
        ):
            raise DomainError(
                "SONIC service_url must be an explicit loopback HTTP port"
            )
        self.origin = self.origin.rstrip("/")
        self.config["preview_url"] = self.origin
        self.web_process = None
        self.snapshot = {"phase": "unavailable", "error": "Service status pending"}
        self.snapshot_at = 0.0
        self.telemetry_snapshot = None
        self.telemetry_snapshot_at = 0.0
        self.capabilities = {
            **super().capabilities,
            "preview_stream": True,
            "native_client_page": True,
            "telemetry": {
                "path": "/api/capture-telemetry",
                "max_signals": 16,
                "history_seconds": 20,
            },
            "service_control": {
                "start_label": "启动数采服务",
                "end_label": "保存完成后停止服务",
                "start_notice": "数采服务正在启动，等待控制程序、遥操与采集器就绪。",
                "end_confirmation": "确认停止本批次数采服务？当前段须先保存完成；随后会退出本批次的控制程序、遥操与采集器。停止服务不是急停。",
                "default_instruction": "pick up the box, carry it to the target location, and put it down",
                "dataset_pattern": "[A-Za-z0-9][A-Za-z0-9_-]{0,63}",
                "dataset_max_length": 64,
                "instruction_max_length": 400,
                "options": [
                    {
                        "id": "raw_only",
                        "label": "保存 Raw，上传后由 Server 转换",
                        "type": "boolean",
                        "default": True,
                        "readonly": True,
                    },
                    {
                        "id": "camera_depth",
                        "label": "启用深度流",
                        "type": "boolean",
                        "default": False,
                    },
                ],
            },
            "episode_commands": [
                {"id": "start", "label": "开始录制", "states": ["IDLE"]},
                {
                    "id": "stop",
                    "label": "停止并保存",
                    "states": ["RECORDING", "PAUSED"],
                },
                {
                    "id": "discard",
                    "label": "丢弃当前段",
                    "states": ["RECORDING", "PAUSED"],
                },
            ],
            "force_end": True,
        }

    def _native_visualizer_source(self):
        if not NATIVE_CLIENT_HTML.is_file():
            raise DomainError("Captured SONIC visualizer HTML is missing")
        html = NATIVE_CLIENT_HTML.read_text(encoding="utf-8")
        if (
            "</body>" not in html
            or "/api/status" not in html
            or "采集控制" not in html
            or "/assets/onboard_collection.js" not in html
        ):
            raise DomainError("Native SONIC visualizer HTML has an unsupported format")
        html = html.replace(
            "/assets/onboard_collection.js",
            "/assets/g1-onboard_collection.js",
        ).replace(
            "/assets/onboard_collection.css",
            "/assets/g1-onboard_collection.css",
        )
        for delimiter in ("'", '"', "`"):
            html = html.replace(
                f"{delimiter}/api/",
                f"{delimiter}/capture-visualizer/api/",
            )
        return html

    def native_visualizer_html(self):
        html = self._native_visualizer_source()
        resize = """
<script>
const reportSonicHeight = () => parent.postMessage(
  {type: "sonic-visualizer-height", height: document.documentElement.scrollHeight},
  location.origin
);
new ResizeObserver(reportSonicHeight).observe(document.documentElement);
addEventListener("load", reportSonicHeight);
</script>
"""
        return html.replace("</body>", resize + "</body>", 1)

    def native_client_html(self):
        """Serve the upstream visualizer as the document and extend it in place."""

        html = self._native_visualizer_source()
        fragment_path = (
            Path(__file__).resolve().parents[2]
            / "web"
            / "sonic-client.html"
        )
        if not fragment_path.is_file():
            raise DomainError("BodyPipeline Client page fragment is missing")
        fragment = fragment_path.read_text(encoding="utf-8")
        if "</head>" not in html or "</body>" not in html:
            raise DomainError("Native SONIC visualizer HTML has an unsupported format")
        html = html.replace(
            "</head>",
            '<link rel="stylesheet" href="/assets/sonic-client.css"></head>',
            1,
        )
        return html.replace("</body>", fragment + "</body>", 1)

    def collection_dataset(self, body):
        return self._http("POST", "/api/collection/dataset", body)

    def telemetry_status(self):
        if (
            self.telemetry_snapshot is not None
            and time.monotonic() - self.telemetry_snapshot_at <= 0.8
        ):
            return self.telemetry_snapshot
        result = self._http("GET", "/api/status")
        self.telemetry_snapshot = result
        self.telemetry_snapshot_at = time.monotonic()
        return result

    def telemetry_signals(self):
        result = self._http("GET", "/api/signals")
        signals = result.get("signals")
        if not isinstance(signals, list) or any(
            not isinstance(signal, str) or not signal or len(signal) > 240
            for signal in signals
        ):
            raise DomainError("SONIC telemetry returned an invalid signal catalog")
        return {"signals": signals[:5000], "truncated": len(signals) > 5000}

    def telemetry_history(self, signals):
        query = urlencode([("signal", signal) for signal in signals])
        result = self._http("GET", "/api/history?" + query)
        series = result.get("series")
        if not isinstance(series, dict) or set(series) - set(signals):
            raise DomainError("SONIC telemetry returned an invalid history payload")
        return result

    @asynccontextmanager
    async def open_preview(self, camera_id):
        camera = next(
            (c for c in self.profile["cameras"] if c["id"] == camera_id), None
        )
        if camera is None:
            raise DomainError("相机不存在", 404)
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as http:
                async with http.stream(
                    "GET",
                    f"{self.origin}/api/camera/{quote(camera['source_key'], safe='')}.mjpg",
                ) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    message = Message()
                    message["Content-Type"] = content_type
                    boundary = message.get_boundary()
                    if (
                        message.get_content_type() != "multipart/x-mixed-replace"
                        or not boundary
                        or len(boundary) > 70
                        or not boundary.isascii()
                        or any(ord(c) < 32 for c in boundary)
                    ):
                        raise DomainError("原生相机未返回有效 MJPEG 视频流", 503)
                    yield PreviewStream(content_type, response.aiter_bytes())
        except httpx.HTTPError as exc:
            raise DomainError(f"相机视频流暂不可用：{type(exc).__name__}", 503) from exc

    def _http(self, method, route, body=None):
        try:
            with httpx.Client(base_url=self.origin, timeout=8, trust_env=False) as http:
                response = http.request(
                    method,
                    route,
                    json=body,
                    headers={"X-Collection-Action": "1"},
                )
            if not response.headers.get("server", "").startswith("SonicWebVisualizer/"):
                raise DomainError("Configured port is not the SONIC Web service")
            result = response.json()
            native_status = (
                isinstance(result, dict)
                and result.get("enabled") is True
                and "phase" in result
            )
            if (
                response.status_code >= 400
                or not isinstance(result, dict)
                or (method != "GET" and result.get("error") and not native_status)
            ):
                raise DomainError(
                    str(result.get("error") if isinstance(result, dict) else result)
                )
            return result
        except httpx.HTTPError as exc:
            raise DomainError(f"SONIC service unavailable: {exc}") from exc
        except (ValueError, TypeError) as exc:
            raise DomainError("Invalid SONIC service response") from exc

    def refresh_service(self):
        try:
            state = self._http("GET", "/api/collection/status")
            if not state.get("enabled") or state.get("raw_only") is not True:
                raise DomainError("SONIC must enable Web control and raw-only mode")
            telemetry = self._http("GET", "/api/status")
            self.telemetry_snapshot = telemetry
            self.telemetry_snapshot_at = time.monotonic()
            if state.get("phase") == "idle":
                self._check_existing_collectors()
            self.snapshot = {
                **state,
                "sources": telemetry.get("sources", {}),
                "telemetry": {
                    key: value
                    for key, value in telemetry.items()
                    if key != "signals"
                },
            }
        except (DomainError, OSError, ValueError) as exc:
            self.snapshot = {"phase": "unavailable", "error": str(exc)}
        self.snapshot_at = time.monotonic()
        return self.snapshot

    def service_status(self):
        if time.monotonic() - self.snapshot_at > 1:
            self.refresh_service()
        state = self.snapshot
        labels = {
            "camera": "相机",
            "robot": "机器人",
            "pico_pose": "PICO",
            "planner": "规划器",
            "manager": "遥操管理器",
        }
        return {
            **state,
            "owned": bool(self.session and self._matches(state)),
            "options": {
                "raw_only": state.get("raw_only", True),
                "camera_depth": state.get("camera", {}).get("depth"),
            },
            "sources": [
                {"id": key, "label": labels.get(key, key), **value}
                for key, value in state.get("sources", {}).items()
            ],
        }

    def background_blocker(self):
        if time.monotonic() - self.snapshot_at > 8:
            return "数采服务状态待刷新，后台处理暂停"
        if self.snapshot.get("phase") != "idle":
            return "数采服务进行中或状态待核实，后台处理暂停"
        return ""

    def _robotics_running(self):
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                executable = (proc / "cmdline").read_bytes().split(b"\0", 1)[0]
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if executable.rsplit(b"/", 1)[-1] == b"RoboticsServiceProcess":
                return True
        return False

    def prepare_service(self):
        self.settings.root.mkdir(parents=True, exist_ok=True)
        script = Path(
            self.config.get(
                "robotics_service_script", "/opt/apps/roboticsservice/runService.sh"
            )
        )
        if not self._robotics_running():
            if not script.is_absolute() or not script.is_file():
                raise DomainError("xrobotservice launcher is missing")
            with (self.settings.root / "robotics-service.log").open("ab") as log:
                launcher = subprocess.Popen(
                    ["/bin/bash", str(script)],
                    cwd=script.parent,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            deadline = time.monotonic() + 5
            while not self._robotics_running():
                launcher.poll()
                if time.monotonic() >= deadline:
                    raise DomainError(
                        "xrobotservice startup not confirmed; inspect robotics-service.log"
                    )
                time.sleep(0.1)
            launcher.poll()
        try:
            with httpx.Client(timeout=2, trust_env=False) as http:
                http.get(self.origin + "/api/collection/status")
        except httpx.ConnectError:
            script = self.repo / "gear_sonic/scripts/run_web_data_visualizer.py"
            if not script.is_file():
                raise DomainError("Native SONIC Web entry point is missing")
            args = [
                str(self.python),
                str(script),
                "--web-host",
                "127.0.0.1",
                "--web-port",
                str(urlsplit(self.origin).port),
                "--enable-web-control",
                "--raw-only",
                "--camera-depth",
                "off",
                "--deploy-checkpoint",
                self.config.get("deploy_checkpoint", "policy/sonic_v1_1/model"),
                "--deploy-obs-config",
                self.config.get(
                    "deploy_obs_config", "policy/sonic_v1_1/observation_config.yaml"
                ),
            ]
            if self.config.get("pose_walk", True):
                args += [
                    "--pose-walk",
                    "--pose-walk-max-speed",
                    str(self.config.get("pose_walk_max_speed", 0.3)),
                ]
            with (self.settings.root / "onboard-web.log").open("ab") as log:
                self.web_process = subprocess.Popen(
                    args,
                    cwd=self.repo,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if self.web_process.poll() is not None:
                    raise DomainError("SONIC Web exited; inspect onboard-web.log")
                if self.refresh_service().get("phase") != "unavailable":
                    break
                time.sleep(0.2)
        except httpx.HTTPError as exc:
            raise DomainError(f"SONIC Web status unknown: {exc}") from exc
        status = self.refresh_service()
        if status.get("phase") == "unavailable":
            raise DomainError(status["error"])
        return self.service_status()

    def _validate_options(self, options):
        if set(options) - {"raw_only", "camera_depth"} or any(
            type(v) is not bool for v in options.values()
        ):
            raise DomainError("Unknown or invalid capture options", 400)
        if options.get("raw_only", True) is not True:
            raise DomainError(
                "This Runtime requires Raw output for Server conversion", 400
            )

    def configure_service(self, options):
        self._validate_options(options)
        status = self.refresh_service()
        if status.get("phase") != "idle" or status.get("session"):
            raise DomainError(
                "Stop the current collection service before changing options"
            )
        if (
            "camera_depth" in options
            and status.get("camera", {}).get("depth") != options["camera_depth"]
        ):
            self._http(
                "POST",
                "/api/collection/camera-depth",
                {"depth": options["camera_depth"]},
            )
        return self.refresh_service()

    def _matches(self, state):
        return (
            self.session is not None
            and state.get("output") == self.session["output"]
            and bool(state.get("session"))
            and (
                not self.session.get("native_session")
                or state["session"] == self.session["native_session"]
            )
        )

    def _owned_state(self):
        state = self.refresh_service()
        if not self._matches(state):
            raise DomainError(
                "Native session ownership does not match this Client batch"
            )
        return state

    def _confirmed(self, operation, body=None):
        nonce = self._http(
            "POST", "/api/collection/challenge", {"operation": operation}
        )["nonce"]
        return self._http(
            "POST", f"/api/collection/{operation}", {**(body or {}), "nonce": nonce}
        )

    def begin(self, batch, persist):
        if self.session:
            raise DomainError("An existing batch must be ended first")
        self.launch_attempted = False
        name = batch["dataset_name"]
        if not NAME.fullmatch(name) or not 1 <= len(batch["instruction"]) <= 400:
            raise DomainError(
                "Dataset name must be 1-64 ASCII letters/digits/_/-; task prompt must be 1-400 characters",
                400,
            )
        self._validate_options(batch.get("capture_options", {}))
        self.prepare_service()
        state = self.configure_service(batch.get("capture_options", {}))
        if state.get("camera", {}).get("ready") is not True:
            raise DomainError(
                "Camera is recovering; wait for RGB readiness before starting"
            )
        self._check_existing_collectors()
        group = batch["id"].replace("-", "")[:16]
        output = self.repo / "outputs" / group / name
        if output.exists() or output.resolve() != output:
            raise DomainError("Native output directory already exists or is unsafe")
        Path(batch["raw_root"]).mkdir(parents=True, exist_ok=True)
        self.session = {
            "onboard": True,
            "output": str(output),
            "service_url": self.origin,
        }
        persist(self.session)
        self.launch_attempted = True
        result = self._confirmed(
            "start",
            {
                "subdirectory": group,
                "dataset_name": name,
                "task_prompt": batch["instruction"],
                "raw_only": True,
                "camera_depth": state["camera"]["depth"],
            },
        )
        if not self._matches(result):
            raise DomainError(
                "SONIC started an unexpected session; ownership needs inspection"
            )
        self.session["native_session"] = result["session"]
        persist(self.session)
        self.refresh_service()
        return self.session

    def recover(self, batch):
        session = batch.get("adapter_session")
        if session and (
            not session.get("onboard") or session.get("service_url") != self.origin
        ):
            raise DomainError(
                "Persisted batch does not match the onboard Adapter configuration"
            )
        self.session = session

    def status(self):
        state = self.refresh_service()
        if not self.session:
            return {"state": "IDLE", "managed": False, "error": state.get("error")}
        if (
            self.session.get("stop_requested")
            and state.get("phase") == "idle"
            and not state.get("session")
        ):
            return {"state": "STOPPED", "service_stopped": True, "managed": True}
        if not self._matches(state):
            return {
                "state": "unknown",
                "managed": True,
                "error": state.get("error")
                or "Native service ownership changed or service exited",
            }
        if state.get("phase") in {"starting", "stopping"}:
            return {
                "state": "STARTING" if state["phase"] == "starting" else "STOPPING",
                "managed": True,
            }
        exporter = state.get("exporter") or {}
        from .g1_sonic import STATES

        return {
            **exporter,
            "state": STATES.get(exporter.get("state"), "unknown"),
            "managed": True,
            "error": state.get("error") or exporter.get("error"),
        }

    def health(self):
        control = self.service_status()
        return {
            "ready": control.get("phase") in {"idle", "running"}
            and not control.get("error")
            and not (control.get("exporter") or {}).get("error"),
            "capture_service": control,
        }

    def command(self, action, request_id):
        if action not in {"start", "stop", "discard"}:
            raise DomainError("Unsupported episode command", 400)
        state = self._owned_state()
        if action == "start" and state.get("phase") != "running":
            raise DomainError("Wait for the collection service to become ready")
        result = self._http(
            "POST", f"/api/collection/episodes/{action}", {"request_id": request_id}
        )
        self.refresh_service()
        return result

    def end(self, force=False):
        state = self.refresh_service()
        if (
            self.session
            and self.session.get("stop_requested")
            and state.get("phase") == "idle"
            and not state.get("session")
        ):
            return True
        state = self._owned_state()
        if state.get("phase") == "stopping":
            return False
        if not force and (state.get("exporter") or {}).get("state") != "idle":
            raise DomainError(
                "Save or discard the current episode before stopping the service"
            )
        self.session["stop_requested"] = True
        self._confirmed("stop")
        self.refresh_service()
        return False

    def observe(self):
        if self.session and not Path(self.session["output"]).exists():
            if self.snapshot.get("phase") in {"starting", "stopping"} or (
                self.session.get("stop_requested")
                and self.snapshot.get("phase") == "idle"
            ):
                return []
        return super().observe()

    def interrupt(self):
        if self.session:
            self.command(
                "discard", "disk-low-" + self.session.get("native_session", "capture")
            )
