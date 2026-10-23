from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .core import digest
from .hdf5 import AGILEX_HDF5_FORMAT, LEGACY_AGILEX_RAW_FORMAT
from .runtime import RobotRuntime


@dataclass
class Settings:
    role: str = "client"
    mode: str = "mock"
    capture_adapter: str = "ros2"
    capture_packager: str = "agilex"
    capture_options: dict = field(default_factory=dict)
    root: Path = field(default_factory=lambda: Path(".runtime/client").resolve())
    data_root: Path | None = None
    import_roots: list[Path] = field(default_factory=list)
    token: str = ""
    login_password: str = ""
    device_token: str = ""
    bridge_token: str = ""
    server_url: str = "http://127.0.0.1:8081"
    bridge_url: str = "http://127.0.0.1:8090"
    robot_id: str = "robot-001"
    project_id: str = "teleop"
    local_roots: dict[str, str] = field(default_factory=dict)
    targets: dict[str, str] = field(default_factory=lambda: {"default": "默认存储"})
    profile_path: str = "configs/agilex-aloha.json"
    profiles: dict[str, dict | str] = field(default_factory=dict)
    default_profile: str | None = None
    min_free_bytes: int = 512 * 1024 * 1024
    chunk_bytes: int = 4 * 1024 * 1024
    max_upload_bytes: int = 100 * 1024**3
    max_files: int = 200000
    max_manifest_bytes: int = 64 * 1024**2
    command_timeout: int = 7200
    letools_bin: str = "letools"
    processing_mode: str = "real"
    auto_process: bool = False
    poll_seconds: float = 2.0
    cookie_secure: bool = False
    viewer_origin: str = "http://127.0.0.1:8082"
    viewer_ui_port: int = 3100
    viewer_data_port: int = 8765
    viewer_ttl: int = 1800
    viewer_bun: str = ""
    argus_src: str = ""
    argus_python: str = ""
    argus_base_url: str = ""
    argus_model: str = "openai/gpt-6-astra"
    argus_api_key_env: str = "PIPELINE_ARGUS_API_KEY"
    argus_auto_label: bool = False
    argus_task_budget_usd: float = 20.0
    argus_daily_budget_usd: float = 100.0
    argus_per_episode_budget_usd: float = 1.0
    argus_max_tokens: int = 12000
    slurm: dict = field(default_factory=dict)
    devices: list[dict] = field(default_factory=list)
    accepted_capture_profile_hashes: list[str] = field(default_factory=list)
    replay: dict = field(default_factory=dict)

    @classmethod
    def load(cls, role: str, path: str | None = None):
        raw = json.loads(Path(path).read_text()) if path else {}
        raw.pop("replay_clients", None)  # Retired Server proxy configuration.
        raw["role"] = role
        raw.setdefault("root", f".runtime/{'server' if role == 'viewer' else role}")
        raw["root"] = Path(raw["root"]).expanduser().resolve()
        if raw.get("data_root") is not None:
            raw["data_root"] = Path(raw["data_root"]).expanduser().resolve()
        raw["import_roots"] = [
            Path(value).expanduser().resolve() for value in raw.get("import_roots", [])
        ]
        for key, env in [
            ("token", "PIPELINE_TOKEN"),
            ("login_password", "PIPELINE_LOGIN_PASSWORD"),
            ("device_token", "PIPELINE_DEVICE_TOKEN"),
            ("bridge_token", "PIPELINE_BRIDGE_TOKEN"),
        ]:
            raw[key] = os.environ.get(env, raw.get(key, ""))
        settings = cls(**raw)
        if len(settings.token) < 24:
            raise ValueError("PIPELINE_TOKEN 必须设置为至少 24 字符的随机密钥")
        if (role == "client" or (role == "server" and not settings.devices)) and len(
            settings.device_token
        ) < 24:
            raise ValueError(
                "PIPELINE_DEVICE_TOKEN 必须设置为至少 24 字符的独立设备密钥"
            )
        if settings.token == settings.device_token:
            raise ValueError("用户密钥与设备密钥必须不同")
        if settings.mode not in {"mock", "ros2"} or settings.processing_mode not in {
            "demo",
            "real",
        }:
            raise ValueError("mode / processing_mode 配置无效")
        if (
            (settings.mode == "ros2" and settings.capture_adapter == "ros2")
            or role == "bridge"
        ) and len(settings.bridge_token) < 24:
            raise ValueError("真实机器人模式必须设置 PIPELINE_BRIDGE_TOKEN")
        if settings.bridge_token and settings.bridge_token in {
            settings.token,
            settings.device_token,
        }:
            raise ValueError("Bridge 密钥必须与用户及设备密钥不同")
        if not 65536 <= settings.chunk_bytes <= 16 * 1024**2:
            raise ValueError("chunk_bytes 应在 64 KiB 到 16 MiB 之间")
        if role == "server" and settings.devices:
            identities, credentials = set(), set()
            for device in settings.devices:
                key = os.environ.get(device["token_env"], "")
                identity = (device["robot_id"], device["project_id"])
                if (
                    len(key) < 24
                    or key == settings.token
                    or key in credentials
                    or identity in identities
                ):
                    raise ValueError("设备注册表必须使用独立有效密钥与不重复身份")
                identities.add(identity)
                credentials.add(key)
        settings._validate_runtime_configuration()
        return settings

    def _validate_runtime_configuration(self):
        if not isinstance(self.profiles, dict):
            raise ValueError("profiles 必须是 runtime_id -> 配置 的对象")
        if self.default_profile is not None and not isinstance(
            self.default_profile, str
        ):
            raise ValueError("default_profile 必须是字符串")
        runtime_ids = set(self.runtime_registry())
        if len(runtime_ids) > 1 and self.default_profile is None:
            raise ValueError("配置了多个 Runtime 时必须设置 default_profile")
        if self.default_profile is not None and self.default_profile not in runtime_ids:
            raise ValueError("default_profile 指向未注册的 Runtime")
        for device in self.devices:
            bound = device.get("runtime_id") or device.get("profile")
            if bound is not None and bound not in runtime_ids:
                raise ValueError(f"设备绑定了未注册的 Runtime：{bound!r}")

    def device_registry(self):
        if self.devices:
            rows = []
            for device in self.devices:
                row = dict(device)
                row.setdefault(
                    "runtime_id",
                    device.get("profile") or self.default_profile or "default",
                )
                rows.append((os.environ.get(device["token_env"], ""), row))
            return rows
        return [
            (
                self.device_token,
                {
                    "robot_id": self.robot_id,
                    "project_id": self.project_id,
                    "targets": list(self.targets),
                    "runtime_id": self.default_profile or "default",
                },
            )
        ]

    def tool_environment(self):
        secret_names = {d["token_env"] for d in self.devices}
        secret_names.update({"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"})
        return {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("PIPELINE_") and key not in secret_names
        }

    def _read_profile(self, path: str | Path) -> dict:
        data = json.loads(Path(path).expanduser().read_text())
        return self._read_profile_value(data)

    def runtime_registry(self) -> dict[str, RobotRuntime]:
        """Load Server-owned Runtime bindings.

        ``profiles`` is the multi-runtime format. The legacy single
        ``profile_path`` remains supported and is exposed as Runtime ``default``.
        """
        if not self.profiles:
            if self.default_profile not in (None, "default"):
                raise ValueError("未配置 profiles 时 default_profile 只能为 default")
            profile = self._read_profile(self.profile_path)
            runtime = RobotRuntime(
                id="default",
                profile=profile,
                profile_hash=digest(profile),
                provider="agilex",
                raw_formats=(AGILEX_HDF5_FORMAT, LEGACY_AGILEX_RAW_FORMAT),
                accepted_capture_profile_hashes=tuple(
                    dict.fromkeys(self.accepted_capture_profile_hashes)
                ),
            )
            return {runtime.id: runtime}

        runtimes = {}
        for runtime_id, spec in self.profiles.items():
            if not isinstance(runtime_id, str) or not runtime_id:
                raise ValueError("Runtime ID 必须是非空字符串")
            if isinstance(spec, str):
                spec = {"profile_path": spec}
            if not isinstance(spec, dict):
                raise ValueError(
                    f"Runtime {runtime_id!r} 配置必须是对象或 Profile 路径"
                )
            profile_path = spec.get("profile_path")
            inline_profile = spec.get("profile")
            if inline_profile is not None:
                if not isinstance(inline_profile, dict):
                    raise ValueError(f"Runtime {runtime_id!r} 的 profile 必须是对象")
                profile = self._read_profile_value(inline_profile)
            elif isinstance(profile_path, str) and profile_path:
                profile = self._read_profile(profile_path)
            else:
                raise ValueError(
                    f"Runtime {runtime_id!r} 需要 profile_path 或内联 profile"
                )
            provider = spec.get("provider", "agilex")
            raw_formats = spec.get("raw_formats")
            if not isinstance(provider, str) or not provider:
                raise ValueError(f"Runtime {runtime_id!r} 的 provider 无效")
            if (
                not isinstance(raw_formats, list)
                or not raw_formats
                or not all(isinstance(x, str) and x for x in raw_formats)
            ):
                raise ValueError(
                    f"Runtime {runtime_id!r} 必须声明非空 raw_formats 列表"
                )
            accepted = list(spec.get("accepted_capture_profile_hashes", []))
            if not all(isinstance(x, str) and len(x) == 64 for x in accepted):
                raise ValueError(
                    f"Runtime {runtime_id!r} 的 accepted_capture_profile_hashes 无效"
                )
            accepted.extend(self.accepted_capture_profile_hashes)
            runtime = RobotRuntime(
                id=runtime_id,
                profile=profile,
                profile_hash=digest(profile),
                provider=provider,
                raw_formats=tuple(raw_formats),
                accepted_capture_profile_hashes=tuple(dict.fromkeys(accepted)),
            )
            runtimes[runtime_id] = runtime
        if len(runtimes) == 1 and self.default_profile is None:
            self.default_profile = next(iter(runtimes))
        return runtimes

    def _read_profile_value(self, data: dict) -> dict:
        if not isinstance(data, dict):
            raise ValueError("机器人 Profile 必须是 JSON 对象")
        copied = json.loads(json.dumps(data))
        if copied.get("control_mode") != "teleoperation":
            raise ValueError("此版本仅支持主从臂遥操作")
        return copied

    def default_runtime(self) -> RobotRuntime:
        registry = self.runtime_registry()
        runtime_id = self.default_profile
        if runtime_id is None:
            if len(registry) != 1:
                raise ValueError("配置了多个 Runtime 时必须设置 default_profile")
            runtime_id = next(iter(registry))
        return registry[runtime_id]

    def profile(self):
        return self.default_runtime().profile
