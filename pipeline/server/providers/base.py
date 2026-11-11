"""Provider interfaces and the trusted runtime identity passed between services."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pipeline.shared.core import DomainError
from pipeline.shared.runtime import RobotRuntime

# Argus names recordings by how they were made, not by robot model. Profiles
# currently describe leader/follower teleoperation; a robot with another
# capture mode may set "annotation_rig" once that mode is supported.
ANNOTATION_RIGS = ("teleop_arms", "handheld_gripper", "ego_head")


def annotation_rig(profile: dict) -> str:
    rig = profile.get("annotation_rig") or "teleop_arms"
    if rig not in ANNOTATION_RIGS:
        # A typo must not silently label an ego-head recording as teleoperation.
        raise DomainError(
            f"Profile 的 annotation_rig 无效：{rig}；"
            f"可选值为 {' / '.join(ANNOTATION_RIGS)}",
            400,
        )
    return rig


def configured_trim_policy(profile: dict) -> dict:
    """Read the optional trusted ``trim`` block a Profile may declare.

    Providers whose data needs different signal semantics or thresholds override
    this method; the Server never guesses them from the robot model.
    """

    config = profile.get("trim")
    if not isinstance(config, dict):
        return {}
    allowed = (
        "feature",
        "candidate_features",
        "unit_thresholds",
        "default_threshold",
        "threshold_per_frame",
        "trim_start",
        "trim_end",
        "min_idle_seconds",
        "min_active_seconds",
        "padding_before_seconds",
        "padding_after_seconds",
        "remove_fully_static",
    )
    return {key: config[key] for key in allowed if key in config}


def configured_interval_policy(profile: dict) -> dict:
    """Read the optional trusted ``static_intervals`` block a Profile declares.

    Whole-recording static detection reuses the signal definition of the
    ``trim`` block (feature, candidate features, units and thresholds). A
    Profile that declares only ``trim`` therefore still gets consistent motion
    semantics.
    """

    signal_keys = (
        "feature",
        "candidate_features",
        "unit_thresholds",
        "default_threshold",
        "threshold_per_frame",
    )
    values: dict = {}
    trim = profile.get("trim")
    if isinstance(trim, dict):
        for key in signal_keys:
            if key in trim:
                values[key] = trim[key]
    config = profile.get("static_intervals")
    if isinstance(config, dict):
        values.update(config)
    allowed = (*signal_keys,)
    return {key: values[key] for key in allowed if key in values}


class RobotProvider(Protocol):
    """Robot-specific conversion boundary.

    Providers are selected only from Server code. Profiles and manifests cannot
    import modules or name arbitrary Python callables.
    """

    id: str

    def validate_profile(self, profile: dict) -> None: ...

    def profile_blockers(self, profile: dict) -> list[dict]: ...

    # Optional hooks: raw_file_contract(raw_format) declares the upload container;
    # validate_upload(raw, manifest, profile) validates bytes before COMMITTED.
    # Providers without these hooks retain the legacy upload behavior.

    def conversion_capabilities(self, profile: dict) -> dict:
        """Describe supported per-job options from trusted Profile configuration."""
        return {}

    def prepare(
        self, raw: Path, dest: Path, profile: dict, *, options: dict | None = None
    ) -> dict:
        """Raise EpisodeValidationError only for identified recording defects.

        Other errors must remain distinct so batch policies cannot hide tool,
        deployment, integrity or implementation failures.
        """
        ...

    def quality(
        self, aligned: Path, profile: dict, synthetic: bool = False
    ) -> dict: ...

    def conversion_args(
        self,
        *,
        executable: str,
        source: Path,
        destination: Path,
        profile: dict,
        job: dict,
    ) -> list[str]: ...

    def patch_dataset_info(
        self,
        dataset: Path,
        profile: dict,
        *,
        synthetic: bool = False,
    ) -> None: ...

    def lineage_metadata(self, profile: dict) -> dict: ...

    def analysis_context(self, profile: dict) -> dict:
        """Facts a downstream analysis engine needs that the dataset does not carry.

        Argus infers camera mounting from camera names and refuses to assume it.
        When the profile states where each camera sits, that fact belongs here so
        the analysis can qualify its cross-modal checks instead of skipping them.
        Nothing in this mapping may be invented: an absent fact stays absent and
        the engine reports the affected check as not assessed.
        """
        return {}

    def annotation_context(self, profile: dict) -> dict:
        """Recording context for the automatic annotation pipeline.

        The model-facing review needs the recording rig and the camera mounting
        facts the dataset itself does not state. Robots with a different capture
        mode say so in their Provider/Profile; the page and API never branch on
        a robot model.
        """
        return {"rig": annotation_rig(profile), "cameras": {}}

    def stationary_trim_policy(self, profile: dict) -> dict:
        """Trusted idle-frame detection configuration, or ``{}`` to use units.

        Boundary trimming must compare movement in the signal that actually
        reflects the robot's motion. A Profile whose action is a velocity or a
        motion token declares that here instead of letting the Server infer it.
        """
        return configured_trim_policy(profile)

    def stationary_interval_policy(self, profile: dict) -> dict:
        """Trusted whole-recording static-interval configuration.

        Removing sustained static spans (including mid-recording pauses) needs
        both motion semantics and its own timing thresholds. Providers that use
        a different signal override this hook; the page and jobs never branch on
        a robot model.
        """
        return configured_interval_policy(profile)


__all__ = [
    "ANNOTATION_RIGS",
    "RobotProvider",
    "RobotRuntime",
    "annotation_rig",
    "configured_interval_policy",
    "configured_trim_policy",
]
