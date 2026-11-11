"""Public, robot-independent options for one immutable conversion request."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pipeline.shared.core import DomainError


class GripperClosureOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    increase_percent: float = Field(strict=True, ge=0, le=100)


class ConversionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gripper_closure: GripperClosureOptions | None = None


def provider_capabilities(provider, profile: dict) -> dict:
    # Providers predating this optional contract continue to accept empty options.
    describe = getattr(provider, "conversion_capabilities", None)
    return describe(profile) if describe else {}


def normalize_options(options: dict | None, capabilities: dict) -> dict:
    try:
        parsed = ConversionOptions.model_validate({} if options is None else options)
    except ValidationError as exc:
        raise DomainError(f"转换选项无效：{exc}", 400) from exc
    result = parsed.model_dump(exclude_none=True)
    for name in result:
        if name not in capabilities:
            raise DomainError(f"所选 Runtime 不支持转换选项：{name}", 400)
    return result
