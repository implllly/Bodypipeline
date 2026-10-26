"""Static adapter registry; never load code named by a request or Manifest."""

from .apex import ApexAdapter
from .mock import MockAdapter
from .ros2 import Ros2Adapter


def create_adapter(settings):
    if settings.mode == "mock":
        if settings.capture_packager != "agilex":
            raise ValueError("Mock capture does not produce real Tianji MCAP")
        return MockAdapter(settings.profile())
    factories = {"ros2": Ros2Adapter, "apex": ApexAdapter}
    if settings.capture_adapter not in factories:
        raise ValueError(f"Unknown capture adapter: {settings.capture_adapter!r}")
    if (settings.capture_adapter == "apex") != (
        settings.capture_packager == "tianji_mcap"
    ):
        raise ValueError(
            "Apex adapter and Tianji MCAP packager must be explicitly paired"
        )
    return factories[settings.capture_adapter](settings)
