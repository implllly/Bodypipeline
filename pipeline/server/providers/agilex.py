"""AgileX ALOHA provider.

This intentionally delegates to the already validated AgileX normalizer. The
provider is an isolation boundary, not a second implementation of the alignment
algorithm.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.shared.core import DomainError, atomic_json
from pipeline.shared.hdf5 import (
    AGILEX_ALIGNED_SCHEMA,
    AGILEX_HDF5_FORMAT,
    LEGACY_AGILEX_RAW_FORMAT,
    find_hdf5_capture,
)

from ..alignment import prepare_agilex, prepare_agilex_hdf5
from ..conversion_options import normalize_options
from ..quality import robot_profile_blockers, robot_quality, validate_robot_policy
from .base import annotation_rig, configured_trim_policy
from .hdf5_preset import write_hdf5_preset

AGILEX_JOINT_DIMENSIONS = 14
AGILEX_RAW_FORMATS = (AGILEX_HDF5_FORMAT, LEGACY_AGILEX_RAW_FORMAT)


class AgileXProvider:
    id = "agilex"
    source_format = "agilex"
    expected_joint_dimensions = AGILEX_JOINT_DIMENSIONS

    def validate_profile(self, profile: dict) -> None:
        if profile.get("robot_type") != "agilex_aloha":
            raise DomainError("AgileX Provider 只接受 robot_type=agilex_aloha")
        validate_robot_policy(profile, expected_dimensions=AGILEX_JOINT_DIMENSIONS)
        self.validate_layout(profile)

    def validate_layout(self, profile: dict) -> None:
        cameras = profile.get("cameras")
        if not isinstance(cameras, list) or [
            camera.get("id") for camera in cameras if isinstance(camera, dict)
        ] != ["left", "front", "right"]:
            raise DomainError("AgileX Profile 需要 left / front / right 三路相机")
        if profile.get("state_streams") != ["puppetLeft", "puppetRight"]:
            raise DomainError(
                "AgileX Profile state_streams 必须为 puppetLeft / puppetRight"
            )
        if profile.get("action_streams") != ["masterLeft", "masterRight"]:
            raise DomainError(
                "AgileX Profile action_streams 必须为 masterLeft / masterRight"
            )

    def profile_blockers(self, profile: dict) -> list[dict]:
        blockers = robot_profile_blockers(
            profile, expected_dimensions=AGILEX_JOINT_DIMENSIONS
        )
        try:
            self.validate_layout(profile)
        except (DomainError, TypeError, ValueError) as exc:
            blockers.append(
                {"field": "provider_profile", "message": "AgileX 配置无效：" + str(exc)}
            )
        return blockers

    def conversion_capabilities(self, profile: dict) -> dict:
        return {}

    def prepare(
        self, raw: Path, dest: Path, profile: dict, *, options: dict | None = None
    ) -> dict:
        normalize_options(options, self.conversion_capabilities(profile))
        package = find_hdf5_capture(raw)
        if package is not None:
            aligned = dest.with_suffix(".hdf5")
            report = prepare_agilex_hdf5(package, aligned, profile)
            return {
                **report,
                "aligned_path": str(aligned),
                "aligned_kind": "hdf5",
                "source_package": package.name,
            }
        report = prepare_agilex(raw, dest, profile)
        return {
            **report,
            "aligned_path": str(dest / "episode0"),
            "aligned_kind": "directory",
        }

    def quality(self, aligned: Path, profile: dict, synthetic: bool = False) -> dict:
        report = robot_quality(
            aligned,
            profile,
            synthetic=synthetic,
            expected_dimensions=AGILEX_JOINT_DIMENSIONS,
        )
        if aligned.is_file():
            import h5py
            import numpy as np

            with h5py.File(aligned, "r") as handle:
                if handle.attrs.get("schema") != AGILEX_ALIGNED_SCHEMA:
                    raise DomainError("对齐 HDF5 schema 不兼容")
                lengths = {
                    len(handle["action"]),
                    len(handle["observation.state"]),
                    *(
                        len(handle[f"observation.images.{camera['id']}"])
                        for camera in profile["cameras"]
                    ),
                }
                if len(lengths) != 1:
                    raise DomainError("对齐 HDF5 各字段帧数不一致")
                for key in ("action", "observation.state"):
                    values = np.asarray(handle[key])
                    if (
                        values.ndim != 2
                        or not np.isfinite(values).all()
                        or values.shape[1] != AGILEX_JOINT_DIMENSIONS
                    ):
                        raise DomainError(f"对齐 HDF5 数值字段无效：{key}")
        return report

    def conversion_args(
        self,
        *,
        executable: str,
        source: Path,
        destination: Path,
        profile: dict,
        job: dict,
    ) -> list[str]:
        common = [
            executable,
            "convert",
            str(source),
            str(destination),
        ]
        hdf5_files = sorted(source.glob("*.hdf5"))
        if hdf5_files:
            preset = self._write_hdf5_preset(source, profile, job, hdf5_files[0])
            source_args = [
                "--source-format",
                "hdf5",
                "--preset",
                str(preset),
            ]
        else:
            source_args = [
                "--source-format",
                self.source_format,
                "--instruction",
                job["instruction"],
                "--fps",
                str(profile["output_fps"]),
                "--robot-type",
                profile["robot_type"],
            ]
        # Conversion is CPU-bound: every episode's frames are re-encoded per
        # camera. A single worker leaves most of an allocation idle, so the
        # worker counts come from configuration and are capped by the CPUs the
        # process can actually use.
        workers = max(1, int(job.get("convert_workers") or 1))
        video_workers = max(1, int(job.get("video_workers") or workers))
        return [
            *common,
            *source_args,
            "--to",
            job.get("target_format", "v3.0"),
            "--workers",
            str(workers),
            "--video-workers",
            str(video_workers),
            "--video-codec",
            "libx264",
            "--video-pixel-format",
            "yuv420p",
            "--video-codec-threads",
            str(max(1, int(job.get("video_codec_threads") or 1))),
        ]

    def _write_hdf5_preset(
        self, source: Path, profile: dict, job: dict, episode: Path
    ) -> Path:
        return write_hdf5_preset(
            source, profile, job, episode, name="bodypipeline_agilex"
        )

    def patch_dataset_info(
        self,
        dataset: Path,
        profile: dict,
        *,
        synthetic: bool = False,
    ) -> None:
        if synthetic:
            return
        info_path = dataset / "meta/info.json"
        info = json.loads(info_path.read_text())
        units = list(profile.get("joint_units") or [])
        for field in ("action", "observation.state"):
            feature = info["features"][field]
            feature["names"] = list(profile["joint_names"])
            # Units are part of the LeRobot feature contract. Downstream tools that
            # qualify a physical layout (for example Argus) refuse to read joint
            # semantics when they are absent, so record what the profile declares.
            if len(units) == len(profile["joint_names"]):
                feature["units"] = units
        atomic_json(info_path, info)

    def lineage_metadata(self, profile: dict) -> dict:
        return {
            "provider": self.id,
            "source_format": self.source_format,
            "raw_formats": list(AGILEX_RAW_FORMATS),
            "joint_dimensions": AGILEX_JOINT_DIMENSIONS,
        }

    def analysis_context(self, profile: dict) -> dict:
        """What an analysis engine cannot read from the dataset itself.

        Only facts the profile actually states are returned. A camera without a
        declared ``mounting`` is left out, so the engine reports the checks that
        depend on it as not assessed rather than guessing.
        """

        cameras = {}
        for camera in profile.get("cameras") or []:
            if not isinstance(camera, dict):
                continue
            mounting = camera.get("mounting")
            camera_id = camera.get("id")
            if isinstance(camera_id, str) and isinstance(mounting, str) and mounting:
                cameras[camera_id] = mounting
        return {
            "cameras": cameras,
            "state_streams": list(profile.get("state_streams") or []),
            "action_streams": list(profile.get("action_streams") or []),
            "joint_names": list(profile.get("joint_names") or []),
            "joint_units": list(profile.get("joint_units") or []),
            "output_fps": profile.get("output_fps"),
            "control_mode": profile.get("control_mode"),
        }

    def stationary_trim_policy(self, profile: dict) -> dict:
        return configured_trim_policy(profile)

    def annotation_context(self, profile: dict) -> dict:
        return {"rig": annotation_rig(profile), **self.analysis_context(profile)}
