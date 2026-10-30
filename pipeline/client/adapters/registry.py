"""Trusted adapter binding; robot-specific formats never route by guessed fields."""

from pipeline.shared.core import DomainError

from .mock import MockAdapter
from .ros2 import Ros2Adapter


def create_adapter(settings, profile):
    name = settings.capture.get("adapter")
    if name is None:
        name = {"mock": "mock", "ros2": "ros2"}.get(settings.mode)
    if name == "mock" and settings.mode == "mock":
        return MockAdapter(profile)
    if name == "ros2" and settings.mode == "ros2":
        return Ros2Adapter(settings)
    if name == "g1_sonic" and settings.mode == "external":
        from .g1_sonic import G1SonicAdapter

        return G1SonicAdapter(settings, profile)
    raise DomainError("未知或不匹配的采集 Adapter / mode 绑定", 500)
