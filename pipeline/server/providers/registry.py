"""Static provider registry and trusted runtime routing."""

from __future__ import annotations

from pipeline.shared.core import DomainError
from pipeline.shared.runtime import RobotRuntime

from .agilex import AgileXProvider
from .aloha_hdf5 import AlohaHDF5Provider
from .base import RobotProvider
from .tianji import TianjiProvider

_PROVIDERS: dict[str, RobotProvider] = {
    "agilex": AgileXProvider(),
    "aloha_hdf5": AlohaHDF5Provider(),
    "tianji": TianjiProvider(),
}


def register_provider(provider: RobotProvider, *, replace: bool = False) -> None:
    """Register a provider from Server code or tests, never from request data."""
    if not isinstance(provider.id, str) or not provider.id:
        raise ValueError("Provider id must be a non-empty string")
    if provider.id in _PROVIDERS and not replace:
        raise ValueError(f"Provider already registered: {provider.id}")
    _PROVIDERS[provider.id] = provider


def get_provider(provider_id: str) -> RobotProvider:
    if not isinstance(provider_id, str) or provider_id not in _PROVIDERS:
        raise DomainError(f"未注册的机器人转换 Provider：{provider_id!r}", 503)
    return _PROVIDERS[provider_id]


def provider_ids() -> tuple[str, ...]:
    return tuple(sorted(_PROVIDERS))


class RuntimeCatalog:
    """Resolve authenticated Server state into a trusted Runtime.

    Runtime records are snapshotted into uploads and jobs. A Client-declared
    robot type or raw format is validated against that binding but never used to
    discover/import Python code.
    """

    def __init__(self, settings):
        self.settings = settings
        self.runtimes = settings.runtime_registry()
        for runtime in self.runtimes.values():
            get_provider(runtime.provider)
        identities = set()
        for _, device in settings.device_registry():
            self.for_device(device)
            identity = (device.get("robot_id"), device.get("project_id"))
            if identity in identities:
                raise DomainError(
                    f"设备注册表包含重复身份：{identity[0]!r}/{identity[1]!r}",
                    503,
                )
            identities.add(identity)

    def all(self) -> list[RobotRuntime]:
        return [self.runtimes[key] for key in sorted(self.runtimes)]

    def public(self) -> list[dict]:
        return [runtime.public() for runtime in self.all()]

    def default(self) -> RobotRuntime:
        runtime_id = self.settings.default_profile
        if runtime_id is None:
            if len(self.runtimes) == 1:
                runtime_id = next(iter(self.runtimes))
            else:
                raise DomainError(
                    "Server 配置了多个 Runtime，但未设置 default_profile", 503
                )
        return self.get(runtime_id)

    def get(self, runtime_id: str | None) -> RobotRuntime:
        if not isinstance(runtime_id, str) or runtime_id not in self.runtimes:
            raise DomainError(f"未注册的机器人 Runtime：{runtime_id!r}", 503)
        return self.runtimes[runtime_id]

    def for_device(self, device: dict) -> RobotRuntime:
        runtime_id = (
            device.get("runtime_id")
            or device.get("profile")
            or self.settings.default_profile
        )
        # Legacy device registries intentionally fall back to the default runtime.
        return self.default() if not runtime_id else self.get(runtime_id)

    def provider(self, runtime: RobotRuntime) -> RobotProvider:
        return get_provider(runtime.provider)

    def _manifest_robot_type(self, manifest: dict) -> str:
        profile = manifest.get("profile")
        if not isinstance(profile, dict):
            raise DomainError("Manifest 缺少 Profile 对象", 400)
        value = profile.get("robot_type")
        if not isinstance(value, str) or not value:
            raise DomainError("Manifest Profile 缺少 robot_type", 400)
        return value

    def for_manifest(
        self,
        manifest: dict,
        *,
        preferred_runtime: str | RobotRuntime | None = None,
    ) -> RobotRuntime:
        if isinstance(preferred_runtime, RobotRuntime):
            return preferred_runtime
        if isinstance(preferred_runtime, str):
            return self.get(preferred_runtime)
        robot_type = self._manifest_robot_type(manifest)
        candidates = [r for r in self.all() if r.robot_type == robot_type]
        if not candidates:
            raise DomainError(
                f"Server 未配置 robot_type={robot_type!r} 的可信 Runtime；"
                "请先部署对应 Provider 与 Profile",
                503,
            )
        profile_hash = manifest.get("profile_hash")
        compatible = [r for r in candidates if r.accepts_profile_hash(profile_hash)]
        if len(compatible) == 1:
            return compatible[0]
        if len(candidates) == 1:
            # Return the type match so the UI/job can explain a hash mismatch.
            return candidates[0]
        raise DomainError(
            f"robot_type={robot_type!r} 对应多个 Runtime，Client 必须由设备注册表绑定 runtime_id",
            503,
        )

    def for_stored_record(self, row: dict, manifest: dict) -> RobotRuntime:
        runtime_id = row.get("runtime_id")
        if runtime_id:
            return self.get(runtime_id)
        # Backward compatibility for uploads/jobs created before runtime IDs existed.
        return self.for_manifest(manifest)

    def validate_capture(
        self,
        runtime: RobotRuntime,
        manifest: dict,
        *,
        require_compatible_hash: bool = True,
    ) -> None:
        robot_type = self._manifest_robot_type(manifest)
        if robot_type != runtime.robot_type:
            raise DomainError(
                f"Manifest robot_type={robot_type!r} 与设备绑定 Runtime "
                f"{runtime.id!r} 的 {runtime.robot_type!r} 不匹配",
                403,
            )
        raw_format = manifest.get("raw_format")
        if raw_format not in runtime.raw_formats:
            raise DomainError(
                f"Runtime {runtime.id!r} 不接受 raw_format={raw_format!r}", 400
            )
        profile_hash = manifest.get("profile_hash")
        if require_compatible_hash and not runtime.accepts_profile_hash(profile_hash):
            raise DomainError(
                "上传 Profile 与服务器批准版本不同；请审核后将兼容摘要加入该 Runtime",
                422,
            )
