"""Tianji/Marvin Pro MCAP provider.

The raw layout is produced by the Tianji client PR and is intentionally kept
as a read-only recording directory::

    episode0/data/metadata.yaml
    episode0/data/*.mcap
    episode0/video/metadata.yaml
    episode0/video/*.mcap

The official tianji-provider package owns MCAP decoding and temporal
alignment.  This Server provider owns the trusted Profile binding, staging,
and the LeTools command line contract.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from pipeline.shared.core import DomainError, atomic_json, executable_path

from ..conversion_options import normalize_options
from ..episode_validation import EpisodeValidationError
from ..quality import robot_profile_blockers, validate_robot_policy
from .base import annotation_rig, configured_trim_policy

TIANJI_RAW_FORMAT = "tianji_mcap_v1"
TIANJI_RAW_FORMAT_ALIASES = (TIANJI_RAW_FORMAT, "tianji_ros2_mcap_v1")
TIANJI_ROBOT_TYPE = "tianji_marvin_pro"
TIANJI_JOINT_DIMENSIONS = 16
TIANJI_PRESET = "thor-v1"
TIANJI_CAMERAS = ("head_left", "head_right", "left_wrist", "right_wrist")


class TianjiProvider:
    id = "tianji"
    source_format = "tianji"
    raw_formats = TIANJI_RAW_FORMAT_ALIASES
    expected_joint_dimensions = TIANJI_JOINT_DIMENSIONS

    def __init__(self) -> None:
        self._provider_bin: str | None = None
        self._timeout = 7200

    def configure(self, settings) -> None:
        """Bind the diagnostic CLI to the same isolated tool environment."""

        configured = getattr(settings, "tianji_provider_bin", "")
        candidate = executable_path(configured) if configured else ""
        if not candidate:
            letools = getattr(settings, "letools_bin", "")
            resolved = executable_path(letools) if letools else None
            if resolved:
                sibling = Path(resolved).with_name("tianji-provider")
                if sibling.is_file() and os.access(sibling, os.X_OK):
                    candidate = str(sibling)
        if not candidate:
            candidate = executable_path("tianji-provider") or ""
        self._provider_bin = candidate or None
        self._timeout = max(1, int(getattr(settings, "command_timeout", 7200)))

    def validate_profile(self, profile: dict) -> None:
        if profile.get("robot_type") != TIANJI_ROBOT_TYPE:
            raise DomainError("Tianji Provider 只接受 robot_type=tianji_marvin_pro")
        validate_robot_policy(profile, expected_dimensions=TIANJI_JOINT_DIMENSIONS)
        self.validate_layout(profile)

    def validate_layout(self, profile: dict) -> None:
        cameras = profile.get("cameras")
        ids = (
            [camera.get("id") for camera in cameras if isinstance(camera, dict)]
            if isinstance(cameras, list)
            else []
        )
        if ids != list(TIANJI_CAMERAS):
            raise DomainError(
                "Tianji Profile 需要 head_left / head_right / left_wrist / right_wrist 四路相机"
            )
        config = profile.get("tianji")
        if not isinstance(config, dict):
            raise DomainError("Tianji Profile 缺少 tianji 配置对象")
        if config.get("preset", TIANJI_PRESET) != TIANJI_PRESET:
            raise DomainError("Tianji Profile 只支持已验证的 thor-v1 preset")
        fps = config.get("fps", profile.get("output_fps", 50))
        wrench_fps = config.get("wrench_fps", 200)
        if type(fps) is not int or not 1 <= fps <= 1000:
            raise DomainError("Tianji fps 必须为 1–1000 的整数")
        if type(wrench_fps) is not int or wrench_fps < fps or wrench_fps % fps:
            raise DomainError("Tianji wrench_fps 必须不小于 fps 且为其整数倍")

    def profile_blockers(self, profile: dict) -> list[dict]:
        blockers = robot_profile_blockers(
            profile, expected_dimensions=TIANJI_JOINT_DIMENSIONS
        )
        try:
            self.validate_layout(profile)
        except (DomainError, TypeError, ValueError) as exc:
            blockers.append(
                {"field": "provider_profile", "message": "Tianji 配置无效：" + str(exc)}
            )
        return blockers

    def conversion_capabilities(self, profile: dict) -> dict:
        return {}

    @staticmethod
    def _recording(raw: Path) -> Path:
        raw = raw.resolve()
        recording = raw / "episode0"
        if recording.is_dir() and not recording.is_symlink():
            return recording
        # Imported/locally staged callers may pass the recording itself.
        if (raw / "data/metadata.yaml").is_file():
            return raw
        raise DomainError("Tianji 原始采集缺少 episode0/data/metadata.yaml")

    @staticmethod
    def _copy_tree(source: Path, destination: Path) -> None:
        if source.is_symlink():
            raise DomainError("Tianji 原始采集不能包含符号链接目录")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source,
            destination,
            symlinks=False,
            copy_function=shutil.copy2,
        )
        for path in destination.rglob("*"):
            if path.is_symlink():
                raise DomainError("Tianji 原始采集不能包含符号链接文件")

    def prepare(
        self, raw: Path, dest: Path, profile: dict, *, options: dict | None = None
    ) -> dict:
        normalize_options(options, self.conversion_capabilities(profile))
        self.validate_profile(profile)
        recording = self._recording(raw)
        staged = dest / "episode0"
        self._copy_tree(recording, staged)
        report = self._inspect(staged, profile)
        return {
            "aligned_path": str(staged),
            "aligned_kind": "directory",
            "frames": report["frames"],
            "fps": report["fps"],
            "wrench_fps": report["wrench_fps"],
            "streams": {
                "state": {"dimensions": [TIANJI_JOINT_DIMENSIONS]},
                "action": {"dimensions": [TIANJI_JOINT_DIMENSIONS]},
                "video": {"dimensions": None},
            },
            "tianji": report,
        }

    def quality(self, aligned: Path, profile: dict, synthetic: bool = False) -> dict:
        self.validate_profile(profile)
        if synthetic:
            return {
                "status": "SYNTHETIC",
                "joint_limits_enforced": False,
                "reason": "模拟数据不能证明 Tianji MCAP 的真实时序和视频有效性",
            }
        report_path = aligned / ".bodypipeline-tianji-inspect.json"
        if report_path.is_file():
            report = json.loads(report_path.read_text())
        else:
            report = self._inspect(aligned, profile)
        return {
            "status": "PASS",
            "joint_limits_enforced": False,
            "scope": "官方 Tianji provider 已完成 schema、时序、对齐和动作覆盖检查",
            "frames": report["frames"],
            "fps": report["fps"],
            "wrench_fps": report["wrench_fps"],
            "reports": report.get("reports", []),
            "units": profile["joint_units"],
        }

    def _provider_command(self) -> str:
        if not self._provider_bin:
            raise DomainError(
                "未找到 tianji-provider；请运行 scripts/install_letools.sh，"
                "并将 letools_bin 指向同一独立环境"
            )
        return self._provider_bin

    def _config_args(self, profile: dict) -> list[str]:
        config = profile.get("tianji", {})
        fps = int(config.get("fps", profile.get("output_fps", 50)))
        args = ["--task", "validation", "--fps", str(fps)]
        for key, option in (
            ("wrench_fps", "--wrench-fps"),
            ("gap_policy", "--gap-policy"),
            ("max_gap_ms", "--max-gap-ms"),
            ("command_max_age_ms", "--command-max-age-ms"),
            ("gripper_max_gap_ms", "--gripper-max-gap-ms"),
            ("state_resampling", "--state-resampling"),
            ("motion_joint_threshold_rad", "--motion-joint-threshold-rad"),
            ("motion_gripper_threshold", "--motion-gripper-threshold"),
        ):
            if key in config:
                args.extend([option, str(config[key])])
        if config.get("trim_stationary", True):
            args.append("--trim-stationary")
        else:
            args.append("--no-trim-stationary")
        return args

    def _inspect(self, source: Path, profile: dict) -> dict:
        report_path = source / ".bodypipeline-tianji-inspect.json"
        command = [
            self._provider_command(),
            "inspect",
            str(source),
            *self._config_args(profile),
            "--report",
            str(report_path),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=self._timeout,
            check=False,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout)[-2400:]
            if result.returncode == 2:
                self._classify_rejection(source, profile, detail)
            raise DomainError(f"Tianji MCAP 核验失败：{detail}")
        try:
            report = json.loads(report_path.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DomainError("tianji-provider 未生成有效 inspect 报告") from exc
        if type(report.get("frames")) is not int or report["frames"] < 1:
            raise DomainError("Tianji inspect 报告缺少有效帧数")
        return report

    def _classify_rejection(self, source: Path, profile: dict, detail: str) -> None:
        """Only structured recording diagnostics can authorize batch skipping.

        inspect uses exit 2 for both validation and tool errors. Never classify
        arbitrary stderr text as a data defect: rerun the read-only checker and
        require its documented exit code and recording-local error schema.
        """
        report_path = source / ".bodypipeline-tianji-check.json"
        report_path.unlink(missing_ok=True)
        result = subprocess.run(
            [
                self._provider_command(),
                "check",
                str(source),
                *self._config_args(profile),
                "--no-deep-video",
                "--workers",
                "1",
                "--report",
                str(report_path),
            ],
            capture_output=True,
            text=True,
            timeout=self._timeout,
            check=False,
        )
        if result.returncode != 1:
            return
        try:
            report = json.loads(report_path.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(report, dict):
            return
        rows = report.get("results")
        if (
            report.get("format") != "tianji-diagnostics-v1"
            or report.get("valid") is not False
            or report.get("recordings") != 1
            or not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], dict)
            or rows[0].get("valid") is not False
        ):
            return
        issues = rows[0].get("issues")
        if not isinstance(issues, list) or not all(isinstance(i, dict) for i in issues):
            return
        errors = [i for i in issues if i.get("severity") == "error"]
        structured_gaps = {"observation_gap", "command_coverage_gap", "video_gap"}
        validation_errors = {
            "required_command_missing",
            "required_signal_missing",
            "alignment_error",
            "numeric_read_error",
            "video_read_error",
            "video_decode_error",
        }
        known_types = {
            "ValueError",
            "RequiredSignalMissing",
            "InvalidDataError",
            "EOFError",
            "UnicodeDecodeError",
            "JSONDecodeError",
        }
        if not errors or not all(
            i.get("code") in structured_gaps
            or (
                i.get("code") in validation_errors
                and i.get("exception_type") in known_types
            )
            for i in errors
        ):
            return
        raise EpisodeValidationError(
            f"Tianji MCAP 核验失败：{detail}",
            code="provider_validation_failed",
            details={"issues": errors, "report_path": str(report_path)},
        )

    def conversion_args(
        self,
        *,
        executable: str,
        source: Path,
        destination: Path,
        profile: dict,
        job: dict,
    ) -> list[str]:
        config = profile.get("tianji", {})
        common = [executable, "convert", str(source), str(destination)]
        args = [
            *common,
            "--source-format",
            self.source_format,
            "--task",
            job["instruction"],
            "--fps",
            str(config.get("fps", profile.get("output_fps", 50))),
            "--wrench-fps",
            str(config.get("wrench_fps", 200)),
            "--to",
            job.get("target_format", "v3.0"),
            "--workers",
            str(max(1, int(job.get("convert_workers") or 1))),
            "--video-workers",
            str(max(1, int(job.get("video_workers") or 1))),
            "--video-codec",
            "libx264",
            "--video-pixel-format",
            "yuv420p",
            "--video-codec-threads",
            str(max(1, int(job.get("video_codec_threads") or 1))),
        ]
        for key, option in (
            ("gap_policy", "--gap-policy"),
            ("max_gap_ms", "--max-gap-ms"),
            ("command_max_age_ms", "--command-max-age-ms"),
            ("gripper_max_gap_ms", "--gripper-max-gap-ms"),
            ("state_resampling", "--state-resampling"),
            ("motion_joint_threshold_rad", "--motion-joint-threshold-rad"),
            ("motion_gripper_threshold", "--motion-gripper-threshold"),
        ):
            if key in config:
                args.extend([option, str(config[key])])
        args.append(
            "--trim-stationary"
            if config.get("trim_stationary", True)
            else "--no-trim-stationary"
        )
        return args

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
        info["robot_type"] = profile["robot_type"]
        for field in ("observation.state", "observation.action"):
            feature = info["features"][field]
            feature["names"] = list(profile["joint_names"])
            feature["units"] = list(profile["joint_units"])
        atomic_json(info_path, info)

    def lineage_metadata(self, profile: dict) -> dict:
        return {
            "provider": self.id,
            "source_format": self.source_format,
            "raw_formats": list(self.raw_formats),
            "joint_dimensions": TIANJI_JOINT_DIMENSIONS,
            "preset": TIANJI_PRESET,
        }

    def analysis_context(self, profile: dict) -> dict:
        cameras = {}
        for camera in profile.get("cameras") or []:
            if not isinstance(camera, dict):
                continue
            camera_id, mounting = camera.get("id"), camera.get("mounting")
            if isinstance(camera_id, str) and isinstance(mounting, str) and mounting:
                cameras[camera_id] = mounting
        return {
            "cameras": cameras,
            "joint_names": list(profile.get("joint_names") or []),
            "joint_units": list(profile.get("joint_units") or []),
            "output_fps": profile.get("output_fps"),
            "control_mode": profile.get("control_mode"),
        }

    def stationary_trim_policy(self, profile: dict) -> dict:
        """Reuse the Tianji provider's verified per-frame motion thresholds."""

        config = profile.get("tianji") or {}
        policy = configured_trim_policy(profile)
        policy.setdefault("feature", "observation.state")
        # Tianji's thresholds are per sample, not per second.
        policy.setdefault("threshold_per_frame", True)
        thresholds = dict(policy.get("unit_thresholds") or {})
        joint = config.get("motion_joint_threshold_rad")
        if isinstance(joint, (int, float)) and not isinstance(joint, bool):
            thresholds.setdefault("rad", float(joint))
        gripper = config.get("motion_gripper_threshold")
        if isinstance(gripper, (int, float)) and not isinstance(gripper, bool):
            thresholds.setdefault("normalized", float(gripper))
        if thresholds:
            policy["unit_thresholds"] = thresholds
        return policy

    def annotation_context(self, profile: dict) -> dict:
        return {"rig": annotation_rig(profile), **self.analysis_context(profile)}


__all__ = [
    "TIANJI_RAW_FORMAT",
    "TIANJI_RAW_FORMAT_ALIASES",
    "TIANJI_ROBOT_TYPE",
    "TianjiProvider",
]
