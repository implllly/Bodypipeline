"""Server conversion for ARX kinesthetic DAgger episodes."""

from __future__ import annotations

import io
import json
import math
import os
from pathlib import Path

import h5py
import numpy as np
from PIL import Image

from pipeline.shared.core import DomainError, atomic_json, digest
from pipeline.shared.hdf5 import AGILEX_ALIGNED_SCHEMA

from ..conversion_options import normalize_options
from ..episode_validation import EpisodeValidationError
from ..quality import robot_profile_blockers, robot_quality, validate_robot_policy
from .aloha_hdf5 import AlohaHDF5Provider
from .base import configured_trim_policy
from .gripper_closure import capabilities, correct_actions
from .hdf5_images import copy_images, inspect_images
from .hdf5_preset import write_hdf5_preset
from .raw_contract import RawFileContract

RAW_FORMAT = "arx_kinesthetic_hdf5_v1"
RAW_SCHEMA = "bodypipeline.arx.kinesthetic.v1"
ACTION_REPRESENTATION = "kinesthetic_next_measured_position"
CAMERAS = ("head", "left_wrist", "right_wrist")
VALID_ACTION_MASK = "action.valid"


def _self_contained(group, seen=None):
    seen = set() if seen is None else seen
    address = h5py.h5o.get_info(group.id).addr
    if address in seen:
        raise EpisodeValidationError("ARX DAgger HDF5 包含循环或别名链接")
    seen.add(address)
    if isinstance(group, h5py.Dataset):
        if group.is_virtual or group.external:
            raise EpisodeValidationError("ARX DAgger HDF5 必须自包含")
        return
    for key in group:
        if not isinstance(group.get(key, getlink=True), h5py.HardLink):
            raise EpisodeValidationError("ARX DAgger HDF5 不允许外部或软链接")
        _self_contained(group[key], seen)


def _image_report(dataset, name: str, frames: int) -> tuple[int, int, str]:
    if dataset.ndim not in (1, 2) or len(dataset) != frames:
        raise EpisodeValidationError(f"ARX DAgger 相机帧数不匹配：{name}")
    declared = str(dataset.attrs.get("encoded_format", "")).lower()
    if declared == "jpg":
        declared = "jpeg"
    if declared not in {"png", "jpeg"}:
        raise EpisodeValidationError(f"ARX DAgger 相机编码未声明或不受支持：{name}")
    size = None
    observed = None

    def inspect_frame(frame):
        payload = frame.tobytes() if isinstance(frame, np.ndarray) else bytes(frame)
        try:
            with Image.open(io.BytesIO(payload)) as image:
                image.load()
                if image.mode != "RGB":
                    raise EpisodeValidationError(f"ARX DAgger 相机不是 RGB：{name}")
                current = image.format
                if current == "JPG":
                    current = "JPEG"
                current = str(current).lower()
                if current not in {"png", "jpeg"} or current != declared:
                    raise EpisodeValidationError(
                        f"ARX DAgger 相机实际编码与声明不一致：{name}"
                    )
                return image.size, current
        except DomainError:
            raise
        except (OSError, ValueError) as exc:
            raise EpisodeValidationError(
                f"ARX DAgger 相机图像无法解码：{name}"
            ) from exc

    for current_size, current in inspect_images(dataset, inspect_frame):
        if size is not None and size != current_size:
            raise EpisodeValidationError(f"ARX DAgger 相机分辨率发生变化：{name}")
        size = current_size
        observed = current
    if size is None or observed is None:
        raise EpisodeValidationError(f"ARX DAgger 相机没有图像：{name}")
    return size[0], size[1], observed


def _read(path: Path, profile: dict, *, exact_profile=False) -> dict:
    try:
        with h5py.File(path, "r") as handle:
            _self_contained(handle)
            if (
                handle.attrs.get("schema") != RAW_SCHEMA
                or handle.attrs.get("raw_format") != RAW_FORMAT
                or handle.attrs.get("complete") != np.bool_(True)
            ):
                raise EpisodeValidationError(
                    "ARX DAgger HDF5 schema、格式或完成标记无效"
                )
            if (
                handle.attrs.get("timebase") != "recorded_monotonic"
                or handle.attrs.get("action_target_offset") != "next_recorded_frame"
                or handle.attrs.get("label_source") != ACTION_REPRESENTATION
            ):
                raise EpisodeValidationError("ARX DAgger 动作时间或标签语义无效")
            if exact_profile and handle.attrs.get("profile_hash") != digest(profile):
                raise EpisodeValidationError("ARX DAgger HDF5 Profile 摘要不一致")
            fps = handle.attrs.get("fps")
            if (
                not isinstance(fps, (int, np.integer))
                or isinstance(fps, bool)
                or int(fps) != profile["output_fps"]
            ):
                raise EpisodeValidationError("ARX DAgger FPS 与 Profile 不一致")
            times = np.asarray(handle["timestamps/monotonic"], dtype=np.float64)
            if (
                times.ndim != 1
                or len(times) < profile["minimum_frames"]
                or not np.isfinite(times).all()
                or np.any(np.diff(times) <= 0)
            ):
                raise EpisodeValidationError("ARX DAgger 时间戳无效或帧数不足")
            frames = len(times)
            state = np.asarray(handle["observation.state"], dtype=np.float32)
            action = np.asarray(handle["action"], dtype=np.float32)
            if (
                state.shape != (frames, 14)
                or action.shape != (frames, 14)
                or not np.isfinite(state).all()
                or not np.isfinite(action).all()
            ):
                raise EpisodeValidationError("ARX DAgger state/action 维度或数值无效")
            if not np.array_equal(action[:-1], state[1:]) or not np.array_equal(
                action[-1], state[-1]
            ):
                raise EpisodeValidationError(
                    "ARX DAgger action 必须严格对应下一帧实测位置"
                )
            steps = np.asarray(handle["action_dt"], dtype=np.float64)
            valid = np.asarray(handle["action_valid_mask"], dtype=bool)
            expected_steps = np.concatenate([np.diff(times), [0.0]])
            expected_valid = (expected_steps > 0) & (expected_steps <= 0.25)
            if (
                steps.shape != (frames,)
                or valid.shape != (frames,)
                or not np.isfinite(steps).all()
                or not np.allclose(steps, expected_steps, rtol=0, atol=1e-12)
                or not np.array_equal(valid, expected_valid)
                or valid[-1]
            ):
                raise EpisodeValidationError(
                    "ARX DAgger action_dt 或 action_valid_mask 与实测时间轴不一致"
                )
            if "source/state" in handle and not np.array_equal(
                np.asarray(handle["source/state"], dtype=np.float32), state
            ):
                raise EpisodeValidationError("ARX DAgger source/state 与观测不一致")
            if "source/captured_at" in handle and not np.array_equal(
                np.asarray(handle["source/captured_at"], dtype=np.float64), times
            ):
                raise EpisodeValidationError(
                    "ARX DAgger source/captured_at 与时间轴不一致"
                )
            cameras = {}
            for name in CAMERAS:
                width, height, encoded = _image_report(
                    handle[f"observation.images.{name}"], name, frames
                )
                cameras[name] = {
                    "width": width,
                    "height": height,
                    "encoded_format": encoded,
                }
            metadata = json.loads(handle["metadata"][()])
            if not isinstance(metadata, dict) or (
                metadata.get("schema") != RAW_SCHEMA
                or metadata.get("raw_format") != RAW_FORMAT
                or metadata.get("session_id") != handle.attrs.get("session_id")
                or metadata.get("profile_hash") != handle.attrs.get("profile_hash")
            ):
                raise EpisodeValidationError("ARX DAgger 文件元数据与采集属性不一致")
    except DomainError:
        raise
    except (KeyError, ValueError, TypeError, OSError) as exc:
        raise EpisodeValidationError(
            f"ARX DAgger 原始文件或图像无法读取：{path.name}"
        ) from exc
    return {
        "frames": frames,
        "times": times - times[0],
        "state": state,
        "action": action,
        "step_s": steps,
        "valid": valid,
        "cameras": cameras,
        "metadata": metadata,
    }


class ARXKinestheticProvider(AlohaHDF5Provider):
    """Preserve next-frame action labels and their validity mask."""

    id = "arx_kinesthetic"
    source_format = RAW_FORMAT
    raw_formats = (RAW_FORMAT,)

    def raw_file_contract(self, raw_format: str) -> RawFileContract:
        if raw_format not in self.raw_formats:
            raise DomainError("ARX DAgger Provider 不接受该原始格式", 400)
        return RawFileContract(single_file=True, filename="episode.hdf5")

    def validate_layout(self, profile: dict) -> None:
        validate_robot_policy(profile, expected_dimensions=14)
        if (
            profile.get("robot_type") != "ARX_X5"
            or profile.get("control_mode") != "teleoperation"
        ):
            raise DomainError("ARX DAgger Provider 需要 ARX_X5 遥操作 Profile")
        if [camera.get("id") for camera in profile.get("cameras", [])] != list(CAMERAS):
            raise DomainError(
                "ARX DAgger 需要 head / left_wrist / right_wrist 三路相机"
            )
        semantics = profile.get("action_semantics")
        if not isinstance(semantics, dict) or (
            semantics.get("representation") != ACTION_REPRESENTATION
            or semantics.get("label_source") != ACTION_REPRESENTATION
            or semantics.get("timebase") != "recorded_monotonic"
            or semantics.get("target_offset") != "next_recorded_frame"
        ):
            raise DomainError("ARX DAgger Profile 的下一帧动作语义不完整")
        if (
            type(profile.get("output_fps")) is not int
            or not 1 <= profile["output_fps"] <= 240
        ):
            raise DomainError("ARX DAgger output_fps 无效")
        if (
            type(profile.get("minimum_frames")) is not int
            or profile["minimum_frames"] < 1
        ):
            raise DomainError("ARX DAgger minimum_frames 无效")
        skew = profile.get("max_frame_skew_ms")
        if (
            type(skew) not in (int, float)
            or not math.isfinite(skew)
            or not 0 < skew <= 1000
        ):
            raise DomainError("ARX DAgger max_frame_skew_ms 无效")

    def validate_profile(self, profile: dict) -> None:
        self.validate_layout(profile)

    def profile_blockers(self, profile: dict) -> list[dict]:
        blockers = robot_profile_blockers(profile, expected_dimensions=14)
        try:
            self.validate_layout(profile)
        except (DomainError, TypeError, ValueError) as exc:
            blockers.append(
                {
                    "field": "provider_profile",
                    "message": "ARX DAgger 配置无效：" + str(exc),
                }
            )
        return blockers

    def conversion_capabilities(self, profile: dict) -> dict:
        return capabilities(profile)

    def episode_metadata(self, path: Path, profile: dict) -> dict:
        self.validate_profile(profile)
        report = _read(path, profile)
        return {
            "frames": report["frames"],
            "dimensions": 14,
            "cameras": {
                key: [value["width"], value["height"]]
                for key, value in report["cameras"].items()
            },
            "max_recorded_gap_ms": float(
                np.max(np.diff(report["times"]), initial=0) * 1000
            ),
            "invalid_action_labels": int(np.count_nonzero(~report["valid"])),
            "metadata": report["metadata"],
        }

    def validate_upload(self, raw: Path, manifest: dict, profile: dict) -> None:
        self.validate_profile(profile)
        try:
            report = _read(
                raw / "episode.hdf5", manifest["profile"], exact_profile=True
            )
        except EpisodeValidationError as exc:
            raise DomainError(str(exc), 422) from exc
        for key in (
            "session_id",
            "robot_id",
            "project_id",
            "profile_hash",
            "dataset_name",
            "task_name",
            "collector_name",
            "instruction",
        ):
            if report["metadata"].get(key) != manifest.get(key):
                raise DomainError(f"ARX DAgger 元数据与 Manifest 不一致：{key}", 422)

    def prepare(
        self, raw: Path, dest: Path, profile: dict, *, options: dict | None = None
    ) -> dict:
        self.validate_profile(profile)
        normalize_options(options, self.conversion_capabilities(profile))
        package = raw if raw.is_file() else raw / "episode.hdf5"
        report = _read(package, profile)
        action = report["action"]
        corrections = {}
        if "gripper_closure" in (options or {}):
            action, corrections["gripper_closure"] = correct_actions(
                report["state"],
                report["action"],
                profile,
                options["gripper_closure"],
            )
        fps = profile["output_fps"]
        frames = int(math.floor(report["times"][-1] * fps + 1e-7)) + 1
        if frames < profile["minimum_frames"]:
            raise EpisodeValidationError("ARX DAgger 时间轴不足以生成最低帧数")
        grid = np.arange(frames, dtype=np.float64) / fps
        right = np.clip(np.searchsorted(report["times"], grid), 0, report["frames"] - 1)
        left = np.maximum(right - 1, 0)
        indices = np.where(
            abs(report["times"][left] - grid) <= abs(report["times"][right] - grid),
            left,
            right,
        )
        frame_skew = abs(report["times"][indices] - grid) * 1000
        # DAgger explicitly permits recording gaps. Repeat the nearest
        # observation for continuity, but invalidate actions beyond the skew
        # bound rather than presenting stale next-frame labels as valid.
        valid = report["valid"][indices] & (frame_skew <= profile["max_frame_skew_ms"])
        if not np.any(valid):
            raise EpisodeValidationError("ARX DAgger 没有有效的下一帧动作")
        aligned = dest.with_suffix(".hdf5")
        aligned.parent.mkdir(parents=True, exist_ok=True)
        temporary = aligned.with_name(f".{aligned.name}.{os.getpid()}.tmp")
        try:
            with (
                h5py.File(temporary, "x") as output,
                h5py.File(package, "r") as source,
            ):
                output.attrs.update(
                    schema=AGILEX_ALIGNED_SCHEMA,
                    fps=fps,
                    frame_count=frames,
                    source_format=RAW_FORMAT,
                    action_target_offset="next_recorded_frame",
                )
                output.create_dataset(
                    "observation.state",
                    data=report["state"][indices],
                    compression="gzip",
                )
                output.create_dataset(
                    "action", data=action[indices], compression="gzip"
                )
                output.create_dataset(
                    VALID_ACTION_MASK,
                    data=valid.astype(np.float32).reshape(-1, 1),
                    compression="gzip",
                )
                output.create_dataset("timestamp", data=grid)
                output.create_dataset("source_frame_index", data=indices)
                for name, camera in report["cameras"].items():
                    dataset = output.create_dataset(
                        f"observation.images.{name}",
                        shape=(frames,),
                        dtype=h5py.vlen_dtype(np.dtype("uint8")),
                    )
                    dataset.attrs.update(
                        width=camera["width"],
                        height=camera["height"],
                        encoded_format=camera["encoded_format"],
                    )
                    copy_images(source[f"observation.images.{name}"], dataset, indices)
            os.replace(temporary, aligned)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return {
            "fps": fps,
            "frames": frames,
            "source_frames": report["frames"],
            "method": "nearest recorded observation with next-frame action mask",
            "alignment_clock": "recorded_monotonic",
            "max_frame_skew_ms": float(frame_skew.max()),
            "max_recorded_gap_ms": float(
                np.max(np.diff(report["times"]), initial=0) * 1000
            ),
            "reused_output_frames": frames - len(np.unique(indices)),
            "invalid_action_labels": int(np.count_nonzero(~valid)),
            "valid_action_labels": int(np.count_nonzero(valid)),
            "streams": {
                "observation.state": {"dimensions": 14},
                "action": {"dimensions": 14},
                VALID_ACTION_MASK: {"dimensions": [1]},
                **{
                    f"observation.images.{key}": {
                        "dimensions": [
                            value["height"],
                            value["width"],
                            3,
                        ]
                    }
                    for key, value in report["cameras"].items()
                },
            },
            "errors": [],
            "aligned_path": str(aligned),
            "aligned_kind": "hdf5",
            "source_package": package.name,
            **({"action_corrections": corrections} if corrections else {}),
        }

    def quality(self, aligned: Path, profile: dict, synthetic: bool = False) -> dict:
        report = robot_quality(aligned, profile, synthetic=synthetic)
        if not aligned.is_file():
            raise DomainError("ARX DAgger 对齐结果缺失")
        with h5py.File(aligned, "r") as handle:
            if handle.attrs.get("schema") != AGILEX_ALIGNED_SCHEMA:
                raise DomainError("ARX DAgger 对齐 HDF5 schema 不兼容")
            lengths = {
                len(handle["action"]),
                len(handle["observation.state"]),
                len(handle[VALID_ACTION_MASK]),
                *(
                    len(handle[f"observation.images.{camera['id']}"])
                    for camera in profile["cameras"]
                ),
            }
            if len(lengths) != 1:
                raise DomainError("ARX DAgger 对齐 HDF5 各字段帧数不一致")
            values = np.asarray(handle[VALID_ACTION_MASK])
            if (
                values.shape != (len(handle["action"]), 1)
                or not np.isfinite(values).all()
                or not np.isin(values, (0, 1)).all()
            ):
                raise EpisodeValidationError("ARX DAgger 动作有效掩码无效")
            valid = int(np.count_nonzero(values))
            if valid == 0:
                raise EpisodeValidationError("ARX DAgger 没有有效动作")
        return {
            **report,
            "action_valid_frames": valid,
            "action_invalid_frames": len(values) - valid,
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
        hdf5_files = sorted(source.glob("*.hdf5"))
        if not hdf5_files:
            raise DomainError("ARX DAgger 对齐结果中没有 HDF5 episode")
        preset = write_hdf5_preset(
            source,
            profile,
            job,
            hdf5_files[0],
            name="bodypipeline_arx_kinesthetic",
            extra_numeric_fields=[
                {
                    "source_key": VALID_ACTION_MASK,
                    "target_key": VALID_ACTION_MASK,
                    "dtype": "float32",
                    "names": ["valid"],
                }
            ],
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
        super().patch_dataset_info(dataset, profile, synthetic=synthetic)
        info_path = dataset / "meta/info.json"
        info = json.loads(info_path.read_text())
        feature = info.get("features", {}).get(VALID_ACTION_MASK)
        if feature is not None:
            feature.update(
                names=["valid"],
                units=["normalized"],
                semantic_type="next_action_valid_mask",
            )
        info["action_semantics"] = dict(profile["action_semantics"])
        atomic_json(info_path, info)

    def lineage_metadata(self, profile: dict) -> dict:
        return {
            **super().lineage_metadata(profile),
            "action_target_offset": "next_recorded_frame",
            "action_valid_mask": VALID_ACTION_MASK,
            "asynchronous_policy": "masked_after_250ms",
        }

    def stationary_trim_policy(self, profile: dict) -> dict:
        return configured_trim_policy(profile)

    def analysis_context(self, profile: dict) -> dict:
        return {
            **super().analysis_context(profile),
            "action_semantics": dict(profile.get("action_semantics") or {}),
            "action_valid_mask": VALID_ACTION_MASK,
        }
