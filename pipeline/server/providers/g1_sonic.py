"""Server binding for direct G1 raw -> LeRobot conversion."""

from __future__ import annotations

import math
import os
import re
from pathlib import Path

from pipeline.shared.core import DomainError, atomic_json, digest

from ..conversion_options import normalize_options
from ..episode_validation import EpisodeValidationError
from ..quality import robot_profile_blockers, validate_robot_policy
from .base import annotation_rig, configured_trim_policy
from .g1_sonic_raw import (
    ACTION_NAMES,
    ACTION_PADDING,
    ACTION_UNITS,
    RAW_FORMAT,
    ROBOT_TYPE,
    SCHEMA,
    STATE_NAMES,
    STATE_PADDING,
    STATE_UNITS,
    index_recording,
    json_object,
    recording_files,
)

SOURCE_DESCRIPTOR = "g1-sonic-source.json"


def numeric_features(profile: dict) -> dict:
    """Feature semantics shared by the Provider and the isolated writer."""
    features = {
        "observation.state": {
            "dtype": "float32",
            "shape": [41],
            "names": list(STATE_NAMES),
            "units": list(STATE_UNITS),
            "semantic_type": "joint_position",
        },
        "action": {
            "dtype": "float32",
            "shape": [76],
            "names": list(ACTION_NAMES),
            "units": list(ACTION_UNITS),
            "semantic_type": "motion_token_and_hand_target",
        },
        "action.wbc": {
            "dtype": "float32",
            "shape": [41],
            "names": list(STATE_NAMES),
            "units": list(STATE_UNITS),
            "semantic_type": "joint_target",
        },
    }
    if profile["g1_sonic"].get("model_layout") == "psi0_45_80":
        for source, target, padding in (
            ("observation.state", "observation.model_state", STATE_PADDING),
            ("action", "action.model", ACTION_PADDING),
        ):
            spec = features[source]
            names, units = iter(spec["names"]), iter(spec["units"])
            dimension = spec["shape"][0] + len(padding)
            features[target] = {
                "dtype": "float32",
                "shape": [dimension],
                "names": [
                    f"padding_{i}" if i in padding else next(names)
                    for i in range(dimension)
                ],
                "units": [
                    "padding" if i in padding else next(units) for i in range(dimension)
                ],
                "semantic_type": "padded_model_input",
                "padding_indices": list(padding),
            }
        features["action.model_mask"] = {
            "dtype": "float32",
            "shape": [80],
            "names": features["action.model"]["names"],
            "semantic_type": "valid_dimension_mask",
        }
    return features


class G1SONICProvider:
    """Support only the explicitly bound SONIC 1.1 / BrainCo raw schema."""

    id = "g1_sonic"
    source_format = RAW_FORMAT
    raw_formats = (RAW_FORMAT,)

    def validate_profile(self, profile: dict) -> None:
        if profile.get("robot_type") != ROBOT_TYPE:
            raise DomainError("G1SONICProvider requires robot_type=g1_brainco")
        validate_robot_policy(profile, expected_dimensions=41)
        if (
            tuple(profile["joint_names"]) != STATE_NAMES
            or tuple(profile["joint_units"]) != STATE_UNITS
        ):
            raise DomainError(
                "G1 Profile must declare the recorded 29 body + 12 BrainCo motor order and units"
            )
        self.validate_layout(profile)

    def validate_layout(self, profile: dict) -> None:
        cfg = profile.get("g1_sonic")
        if (
            not isinstance(cfg, dict)
            or cfg.get("sonic_version") != "1.1"
            or cfg.get("hand_type") != "brainco"
        ):
            raise DomainError(
                "G1 Profile requires g1_sonic.sonic_version=1.1 and hand_type=brainco"
            )
        if cfg.get("model_layout", "native") not in {"native", "psi0_45_80"}:
            raise DomainError("Unsupported G1 model_layout")
        if cfg.get("camera_time_fallback", "forbid") not in {"forbid", "mapped_wall"}:
            raise DomainError("Unsupported G1 camera_time_fallback")
        for key in ("robot_max_age_ms", "camera_max_age_ms"):
            value = cfg.get(key)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 < value <= 60_000
            ):
                raise DomainError(
                    f"G1 {key} must be a finite source age budget in milliseconds"
                )
        fps = profile.get("output_fps")
        if type(fps) is not int or not 1 <= fps <= 240:
            raise DomainError("G1 output_fps must be an integer in [1,240]")
        minimum, maximum = (
            profile.get("minimum_frames", 2),
            cfg.get("max_frames", 1_000_000),
        )
        if (
            type(minimum) is not int
            or type(maximum) is not int
            or not 1 <= minimum <= maximum
        ):
            raise DomainError("G1 minimum_frames/max_frames are invalid")
        cameras = profile.get("cameras")
        if not isinstance(cameras, list) or not cameras:
            raise DomainError(
                "G1 Profile requires at least one explicit camera mapping"
            )
        ids, sources = set(), set()
        for camera in cameras:
            if not isinstance(camera, dict):
                raise DomainError("G1 camera must be an object")
            camera_id, source = camera.get("id"), camera.get("source_key")
            if (
                not isinstance(camera_id, str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", camera_id)
                or camera_id in ids
            ):
                raise DomainError(
                    "G1 camera ids must be unique safe feature identifiers"
                )
            if (
                source not in {"ego_view", "left_wrist", "right_wrist"}
                or source in sources
            ):
                raise DomainError(
                    "G1 camera source_key must explicitly name a unique recorded camera"
                )
            ids.add(camera_id)
            sources.add(source)
            for key in ("width", "height"):
                value = camera.get(key)
                if type(value) is not int or not 2 <= value <= 8192 or value % 2:
                    raise DomainError(
                        f"G1 camera {key} must be positive/even for H.264 yuv420p"
                    )

    def profile_blockers(self, profile: dict) -> list[dict]:
        blockers = robot_profile_blockers(profile, expected_dimensions=41)
        try:
            self.validate_profile(profile)
        except (DomainError, TypeError, ValueError) as exc:
            blockers.append({"field": "provider_profile", "message": str(exc)})
        return blockers

    def conversion_capabilities(self, profile: dict) -> dict:
        return {}

    def prepare(
        self, raw: Path, dest: Path, profile: dict, *, options: dict | None = None
    ) -> dict:
        normalize_options(options, self.conversion_capabilities(profile))
        self.validate_profile(profile)
        path, episode, config = recording_files(raw)
        index = index_recording(path, episode, profile, config)
        if index.report["frames"] < profile.get("minimum_frames", 2):
            raise EpisodeValidationError(
                "G1 episode is shorter than Profile minimum_frames"
            )
        staged = dest / "episode0"
        staged.mkdir(parents=True, exist_ok=False)
        atomic_json(
            staged / SOURCE_DESCRIPTOR,
            {
                "schema": RAW_FORMAT,
                "profile_hash": digest(profile),
                "raw_path": str(index.path),
                "episode": episode,
                "robot_config": index.robot_config,
                "report": index.report,
            },
        )
        return {
            **index.report,
            "aligned_path": str(staged),
            "aligned_kind": "directory",
        }

    def quality(self, aligned: Path, profile: dict, synthetic: bool = False) -> dict:
        self.validate_profile(profile)
        descriptor = json_object(aligned / SOURCE_DESCRIPTOR)
        if descriptor.get("schema") != RAW_FORMAT or descriptor.get(
            "profile_hash"
        ) != digest(profile):
            raise DomainError("G1 prepared source/Profile binding mismatch")
        report = descriptor["report"]
        return {
            "status": "SYNTHETIC"
            if synthetic
            else ("WARN" if report["warnings"] else "PASS"),
            "joint_limits_enforced": False,
            "schema": SCHEMA,
            "scope": "CRC/SHA256, recorded schema, finite vectors, causal alignment, JPEG decoding; no token-to-joint inference",
            "warnings": report["warnings"],
            "frames": report["frames"],
            "features": numeric_features(profile),
        }

    def conversion_args(
        self,
        *,
        executable: str,
        source: Path,
        destination: Path,
        profile: dict,
        job: dict,
    ) -> list[str]:
        self.validate_profile(profile)
        python = Path(executable).absolute().with_name("python")
        if not python.is_file() or not os.access(python, os.X_OK):
            raise DomainError(
                "G1 direct conversion requires Python next to letools_bin"
            )
        profile_path = source.parent / "g1-sonic-profile.json"
        atomic_json(profile_path, profile)
        return [
            str(python),
            str(Path(__file__).with_name("g1_sonic_source.py").absolute()),
            str(source),
            str(destination),
            "--profile",
            str(profile_path),
            "--task",
            job["instruction"],
            "--to",
            job.get("target_format", "v3.0"),
            "--workers",
            str(max(1, int(job.get("convert_workers") or 1))),
            "--video-workers",
            str(max(1, int(job.get("video_workers") or 1))),
            "--video-codec-threads",
            str(max(1, int(job.get("video_codec_threads") or 1))),
        ]

    def patch_dataset_info(
        self, dataset: Path, profile: dict, *, synthetic: bool = False
    ) -> None:
        self.validate_profile(profile)
        path = dataset / "meta/info.json"
        info = json_object(path)
        if info.get("robot_type") != ROBOT_TYPE:
            raise DomainError("G1 output robot_type disagrees with Provider binding")
        for key, spec in numeric_features(profile).items():
            feature = info.get("features", {}).get(key)
            if not isinstance(feature, dict) or feature.get("shape") != spec["shape"]:
                raise DomainError(f"G1 output feature missing or wrong shape: {key}")
            feature.update(spec)
        info["g1_sonic"] = self.lineage_metadata(profile)
        if synthetic:
            info["synthetic"] = True
        atomic_json(path, info)
        atomic_json(
            dataset / "meta/modality.json",
            {
                "schema": SCHEMA,
                "sonic_version": "1.1",
                "hand_type": "brainco",
                "features": numeric_features(profile),
                "state_padding": list(STATE_PADDING)
                if profile["g1_sonic"].get("model_layout") == "psi0_45_80"
                else [],
                "action_padding": list(ACTION_PADDING)
                if profile["g1_sonic"].get("model_layout") == "psi0_45_80"
                else [],
            },
        )

    def lineage_metadata(self, profile: dict) -> dict:
        return {
            "provider": self.id,
            "source_format": RAW_FORMAT,
            "raw_formats": list(self.raw_formats),
            "schema": SCHEMA,
            "sonic_version": "1.1",
            "hand_type": "brainco",
            "state_dimensions": 41,
            "action_dimensions": 76,
            "model_layout": profile["g1_sonic"].get("model_layout", "native"),
            "alignment": "latest-not-future",
            "intermediate_format": None,
        }

    def analysis_context(self, profile: dict) -> dict:
        return {
            "cameras": {
                c["id"]: c["mounting"] for c in profile["cameras"] if c.get("mounting")
            },
            "signal_features": numeric_features(profile),
            "output_fps": profile["output_fps"],
            "control_mode": profile.get("control_mode"),
        }

    def stationary_trim_policy(self, profile: dict) -> dict:
        return configured_trim_policy(profile)

    def annotation_context(self, profile: dict) -> dict:
        return {"rig": annotation_rig(profile), **self.analysis_context(profile)}
