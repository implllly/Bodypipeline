"""Provider for upstream ALOHA-style raw HDF5 episodes.

Some dataloggers (ARX and other ALOHA derivatives) write one HDF5 file per
episode whose fields are already uniformly sampled: ``observations/qpos``,
``action`` and ``observations/images/<camera>`` as JPEG byte arrays. There are
no per-stream timestamps, so there is nothing to resample: alignment is a
schema translation into the same aligned layout the AgileX path produces.

The layout is declared by the Profile, never inferred from the file. A field
that the Profile names but the file lacks is an error, not a silent skip.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import numpy as np

from pipeline.shared.core import DomainError, atomic_json
from pipeline.shared.hdf5 import AGILEX_ALIGNED_SCHEMA, ALOHA_HDF5_FORMAT

from ..conversion_options import normalize_options
from ..episode_validation import EpisodeValidationError
from ..quality import robot_profile_blockers, robot_quality, validate_robot_policy
from .base import annotation_rig, configured_trim_policy
from .gripper_closure import capabilities, correct_actions
from .hdf5_images import copy_images
from .hdf5_preset import write_hdf5_preset

DEFAULT_STATE_KEY = "observations/qpos"
DEFAULT_ACTION_KEY = "action"


def _field(profile: dict, name: str, default: str) -> str:
    value = profile.get(name, default)
    if not isinstance(value, str) or not value:
        raise DomainError(f"Profile 的 {name} 必须是非空 HDF5 字段名")
    return value


def _camera_fields(profile: dict) -> list[tuple[str, str]]:
    cameras = profile.get("cameras")
    if not isinstance(cameras, list) or not cameras:
        raise DomainError("Profile 需要至少一路相机")
    fields = []
    for camera in cameras:
        if not isinstance(camera, dict):
            raise DomainError("Profile 相机必须是对象")
        camera_id, source_key = camera.get("id"), camera.get("source_key")
        if not isinstance(camera_id, str) or not camera_id:
            raise DomainError("Profile 相机缺少 id")
        if not isinstance(source_key, str) or not source_key:
            raise DomainError(f"相机 {camera_id!r} 缺少 source_key")
        fields.append((camera_id, source_key))
    return fields


def _frames(dataset) -> int:
    return int(dataset.shape[0]) if dataset.ndim >= 1 else 0


def _encoded_frame(dataset, index: int) -> bytes:
    value = dataset[index]
    return value.tobytes() if isinstance(value, np.ndarray) else bytes(value)


def _jpeg_size(payload: bytes, label: str) -> tuple[int, int]:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            if image.format != "JPEG":
                raise EpisodeValidationError(f"相机 {label} 不是 JPEG 编码")
            return image.size
    except DomainError:
        raise
    except (OSError, ValueError) as exc:
        raise EpisodeValidationError(f"相机 {label} 图像无法解码") from exc


def _read_episode(path: Path, profile: dict) -> dict:
    """Validate one raw episode and return its frames, arrays and camera size."""

    import h5py

    state_key = _field(profile, "state_key", DEFAULT_STATE_KEY)
    action_key = _field(profile, "action_key", DEFAULT_ACTION_KEY)
    cameras = _camera_fields(profile)
    try:
        with h5py.File(path, "r") as handle:
            for key in (state_key, action_key):
                if key not in handle:
                    raise EpisodeValidationError(f"{path.name} 缺少字段 {key}")
            state = np.asarray(handle[state_key], dtype=np.float32)
            action = np.asarray(handle[action_key], dtype=np.float32)
            if state.ndim != 2 or action.ndim != 2:
                raise EpisodeValidationError(f"{path.name} 的 state/action 必须是二维数组")
            if state.shape != action.shape:
                raise EpisodeValidationError(f"{path.name} 的 state/action 形状不一致")
            if not np.isfinite(state).all() or not np.isfinite(action).all():
                raise EpisodeValidationError(f"{path.name} 的 state/action 含非有限值")
            names = profile.get("joint_names") or []
            if names and len(names) != state.shape[1]:
                raise EpisodeValidationError(
                    f"{path.name} 的关节维度 {state.shape[1]} 与 Profile 的 "
                    f"joint_names 长度 {len(names)} 不一致"
                )
            frames = state.shape[0]
            if frames < 1:
                raise EpisodeValidationError(f"{path.name} 没有帧")
            shapes = {}
            for camera_id, source_key in cameras:
                if source_key not in handle:
                    raise EpisodeValidationError(f"{path.name} 缺少相机字段 {source_key}")
                dataset = handle[source_key]
                if _frames(dataset) != frames:
                    raise EpisodeValidationError(
                        f"{path.name} 相机 {camera_id} 帧数与 state 不一致"
                    )
                size = _jpeg_size(_encoded_frame(dataset, 0), camera_id)
                width = int(dataset.attrs.get("width", 0)) or size[0]
                height = int(dataset.attrs.get("height", 0)) or size[1]
                if (width, height) != size:
                    raise EpisodeValidationError(
                        f"{path.name} 相机 {camera_id} 分辨率与声明不一致"
                    )
                shapes[camera_id] = (width, height)
    except DomainError:
        raise
    except (KeyError, ValueError) as exc:
        raise EpisodeValidationError(f"HDF5 原始 episode 无法读取：{path.name}") from exc
    return {
        "frames": frames,
        "dimensions": int(state.shape[1]),
        "cameras": shapes,
        "state": state,
        "action": action,
        "state_key": state_key,
        "action_key": action_key,
    }


class AlohaHDF5Provider:
    """Translate uniformly sampled upstream HDF5 episodes for letools."""

    id = "aloha_hdf5"
    source_format = "aloha_hdf5"
    raw_formats = (ALOHA_HDF5_FORMAT,)

    def validate_profile(self, profile: dict) -> None:
        validate_robot_policy(profile)
        self.validate_layout(profile)

    def validate_layout(self, profile: dict) -> None:
        _field(profile, "state_key", DEFAULT_STATE_KEY)
        _field(profile, "action_key", DEFAULT_ACTION_KEY)
        _camera_fields(profile)
        self.conversion_capabilities(profile)
        fps = profile.get("output_fps")
        if type(fps) is not int or not 1 <= fps <= 240:
            raise DomainError("Profile 的 output_fps 必须是 1–240 的整数")

    def profile_blockers(self, profile: dict) -> list[dict]:
        blockers = robot_profile_blockers(profile)
        try:
            self.validate_layout(profile)
        except (DomainError, TypeError, ValueError) as exc:
            blockers.append(
                {"field": "provider_profile", "message": "HDF5 配置无效：" + str(exc)}
            )
        return blockers

    def episode_metadata(self, path: Path, profile: dict) -> dict:
        """Validate one raw episode at import time without keeping it in memory."""

        report = _read_episode(path, profile)
        return {
            "frames": report["frames"],
            "dimensions": report["dimensions"],
            "cameras": {k: list(v) for k, v in report["cameras"].items()},
        }

    def conversion_capabilities(self, profile: dict) -> dict:
        return capabilities(profile)

    def prepare(
        self, raw: Path, dest: Path, profile: dict, *, options: dict | None = None
    ) -> dict:
        options = normalize_options(options, self.conversion_capabilities(profile))
        package = raw if raw.is_file() else self._single_file(raw)
        report = _read_episode(package, profile)
        corrections = {}
        if "gripper_closure" in options:
            report["action"], corrections["gripper_closure"] = correct_actions(
                report["state"], report["action"], profile, options["gripper_closure"]
            )
        dimensions = report["dimensions"]
        aligned = dest.with_suffix(".hdf5")
        aligned.parent.mkdir(parents=True, exist_ok=True)
        temporary = aligned.with_name(f".{aligned.name}.{os.getpid()}.tmp")
        temporary.unlink(missing_ok=True)
        import h5py

        frames = report["frames"]
        try:
            with h5py.File(temporary, "w") as output:
                output.attrs["schema"] = AGILEX_ALIGNED_SCHEMA
                output.attrs["fps"] = profile["output_fps"]
                output.attrs["frame_count"] = frames
                output.attrs["source_format"] = ALOHA_HDF5_FORMAT
                output.create_dataset(
                    "observation.state", data=report["state"], compression="gzip"
                )
                output.create_dataset(
                    "action", data=report["action"], compression="gzip"
                )
                for camera_id, source_key in _camera_fields(profile):
                    width, height = report["cameras"][camera_id]
                    dataset = output.create_dataset(
                        f"observation.images.{camera_id}",
                        shape=(frames,),
                        dtype=h5py.vlen_dtype(np.dtype("uint8")),
                    )
                    dataset.attrs["width"] = width
                    dataset.attrs["height"] = height
                    dataset.attrs["encoded_format"] = "jpeg"
                    with h5py.File(package, "r") as source:
                        copy_images(source[source_key], dataset)
            os.replace(temporary, aligned)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return {
            "fps": profile["output_fps"],
            "frames": frames,
            "method": "uniform upstream sampling; HDF5 schema translation",
            "streams": {
                "observation.state": {"dimensions": dimensions},
                "action": {"dimensions": dimensions},
                **{
                    f"observation.images.{camera_id}": {
                        "dimensions": [height, width, 3]
                    }
                    for camera_id, (width, height) in report["cameras"].items()
                },
            },
            "errors": [],
            "aligned_path": str(aligned),
            "aligned_kind": "hdf5",
            "source_package": package.name,
            **({"action_corrections": corrections} if corrections else {}),
        }

    @staticmethod
    def _single_file(raw: Path) -> Path:
        from pipeline.shared.hdf5 import find_hdf5_capture

        package = find_hdf5_capture(raw)
        if package is None:
            raise DomainError("没有找到 HDF5 episode 文件")
        return package

    def quality(self, aligned: Path, profile: dict, synthetic: bool = False) -> dict:
        report = robot_quality(aligned, profile, synthetic=synthetic)
        if aligned.is_file():
            import h5py

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
        hdf5_files = sorted(source.glob("*.hdf5"))
        if not hdf5_files:
            raise DomainError("对齐结果中没有可转换的 HDF5 episode")
        preset = write_hdf5_preset(
            source, profile, job, hdf5_files[0], name="bodypipeline_aloha_hdf5"
        )
        workers = max(1, int(job.get("convert_workers") or 1))
        video_workers = max(1, int(job.get("video_workers") or workers))
        return [
            executable,
            "convert",
            str(source),
            str(destination),
            "--source-format",
            "hdf5",
            "--preset",
            str(preset),
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
            # Argus refuses to read joint semantics when units are absent.
            if len(units) == len(profile["joint_names"]):
                feature["units"] = units
        atomic_json(info_path, info)

    def lineage_metadata(self, profile: dict) -> dict:
        return {
            "provider": self.id,
            "source_format": self.source_format,
            "raw_formats": list(self.raw_formats),
            "joint_dimensions": len(profile.get("joint_names") or []),
        }

    def analysis_context(self, profile: dict) -> dict:
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
            "state_streams": [],
            "action_streams": [],
            "joint_names": list(profile.get("joint_names") or []),
            "joint_units": list(profile.get("joint_units") or []),
            "output_fps": profile.get("output_fps"),
            "control_mode": profile.get("control_mode"),
        }

    def stationary_trim_policy(self, profile: dict) -> dict:
        return configured_trim_policy(profile)

    def annotation_context(self, profile: dict) -> dict:
        return {"rig": annotation_rig(profile), **self.analysis_context(profile)}
