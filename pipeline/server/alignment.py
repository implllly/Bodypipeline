from __future__ import annotations

import bisect
import io
import json
import math
import os
from pathlib import Path

from PIL import Image

from pipeline.shared.core import DomainError, atomic_json, fsync_dir

from .episode_validation import EpisodeValidationError


def series(path: Path, ext: str):
    files = list(path.glob("*" + ext))
    pairs = []
    for p in files:
        try:
            t = float(p.stem)
        except ValueError as exc:
            raise EpisodeValidationError(f"无效时间戳：{p.name}") from exc
        if not math.isfinite(t) or p.is_symlink():
            raise DomainError("原始时间序列有非法时间戳或符号链接")
        pairs.append((t, p))
    pairs.sort()
    if not pairs or len({t for t, _ in pairs}) != len(pairs):
        raise EpisodeValidationError(f"空或重复时间序列：{path.name}")
    return [t for t, _ in pairs], [p for _, p in pairs]


def prepare_agilex(raw: Path, dest: Path, profile: dict):
    """Build a bounded, time-aligned local staging view; never rewrite uploaded raw.

    All views use the same grid timestamp, making the built-in AgileX reader's
    index pairing exact. Source images/JSON remain immutable; symlinks exist only
    in ephemeral input staging, never in the converted LeRobot output.
    """
    if dest.exists():
        raise DomainError("对齐输出目录已存在，必须使用新的尝试目录")
    fps = profile["output_fps"]
    camera_bound = profile["max_camera_skew_ms"] / 1000
    joint_bound = profile["max_action_age_ms"] / 1000
    if (
        type(fps) is not int
        or not 1 <= fps <= 240
        or not 0 < camera_bound <= 1
        or not 0 < joint_bound <= 1
    ):
        raise DomainError("不合法的对齐策略")
    if (
        [x["id"] for x in profile["cameras"]] != ["left", "front", "right"]
        or profile["state_streams"] != ["puppetLeft", "puppetRight"]
        or profile["action_streams"] != ["masterLeft", "masterRight"]
    ):
        raise DomainError(
            "本版 AgileX normalizer 仅支持已审查的三相机主从臂布局；其他机器人需新 source adapter"
        )
    episode = raw / "episode0"
    streams = {}
    for camera in profile["cameras"]:
        rel = f"camera/color/{camera['id']}"
        streams[rel] = (*series(episode / rel, ".jpg"), True)
    for role in profile["state_streams"] + profile["action_streams"]:
        rel = f"arm/jointState/{role}"
        streams[rel] = (*series(episode / rel, ".json"), False)
    start = max(v[0][0] for v in streams.values())
    stop = min(v[0][-1] for v in streams.values())
    n = math.floor((stop - start) * fps + 1e-5) + 1
    if n < profile["minimum_frames"] or n > 10_000_000:
        raise EpisodeValidationError(f"有效公共区间过短/过大：{n} 帧")
    dest.mkdir(parents=True)
    report = {
        "fps": fps,
        "frames": n,
        "source_start": start,
        "source_end": stop,
        "duration": n / fps,
        "streams": {},
        "method": "uniform grid; bounded nearest camera; causal bounded joint hold",
        "errors": [],
    }
    for rel, (times, paths, camera) in streams.items():
        output = dest / "episode0" / rel
        output.mkdir(parents=True)
        offsets, used, previous, repeats = [], set(), None, 0
        shape = None
        for i in range(n):
            t = start + i / fps
            if camera:
                j = bisect.bisect_left(times, t)
                candidates = [v for v in (j - 1, j) if 0 <= v < len(times)]
                j = min(candidates, key=lambda x: abs(times[x] - t))
                offset = abs(times[j] - t)
                bound = camera_bound
            else:
                j = bisect.bisect_right(times, t) - 1
                if j < 0:
                    raise EpisodeValidationError("没有因果前置关节样本")
                offset, bound = t - times[j], joint_bound
            if offset > bound:
                raise EpisodeValidationError(
                    f"时间对齐越界：{rel} frame={i} error={offset * 1000:.3f}ms limit={bound * 1000:.3f}ms"
                )
            source = paths[j]
            # Decode/validate every selected source once. Corrupt media cannot pass by filename alone.
            if j not in used:
                if camera:
                    # File reads stay outside decoding so filesystem failures
                    # cannot become skippable image-content rejections.
                    encoded = source.read_bytes()
                    try:
                        with Image.open(io.BytesIO(encoded)) as image:
                            image.load()
                            if shape is None:
                                shape = image.size
                            if image.size != shape or image.mode != "RGB":
                                raise EpisodeValidationError(
                                    f"相机分辨率或颜色模式不一致：{rel}"
                                )
                    except (OSError, ValueError) as exc:
                        raise EpisodeValidationError(f"相机图像损坏：{rel}") from exc
                else:
                    text = source.read_text()
                    try:
                        document = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise EpisodeValidationError(f"关节 JSON 无效：{rel}") from exc
                    values = (
                        document.get("position") if isinstance(document, dict) else None
                    )
                    if (
                        not isinstance(values, list)
                        or len(values) != 7
                        or not all(
                            type(v) in (float, int) and math.isfinite(v) for v in values
                        )
                    ):
                        raise EpisodeValidationError(
                            f"关节数据要求 7 个有限 position：{rel}"
                        )
                used.add(j)
            repeats += previous == j
            previous = j
            offsets.append(offset * 1000)
            # Preserve local grid in all filenames, no cumulative floating point increments.
            name = f"{i / fps:.9f}" + (".jpg" if camera else ".json")
            os.symlink(source.resolve(), output / name)
        offsets.sort()
        report["streams"][rel] = {
            "input_frames": len(paths),
            "selected_unique": len(used),
            "repeated_frames": repeats,
            "max_offset_ms": max(offsets),
            "p95_offset_ms": offsets[min(len(offsets) - 1, int(0.95 * len(offsets)))],
            "dimensions": shape,
        }
    atomic_json(dest.parent / "alignment-report.json", report)
    return report


def prepare_agilex_hdf5(raw: Path, dest: Path, profile: dict):
    """Align timestamped streams inside one raw HDF5 file into one aligned HDF5.

    The uploaded package is never extracted. The aligned file is written once
    with Letools-native numeric/video field names and can be consumed directly
    by the mapping-driven ``letools convert --source-format hdf5`` provider.
    """

    import h5py
    import numpy as np

    from pipeline.shared.hdf5 import (
        AGILEX_ALIGNED_SCHEMA,
        AGILEX_HDF5_SCHEMA,
        read_package_metadata,
    )

    if dest.exists():
        raise DomainError("HDF5 对齐输出已存在，必须使用新的尝试目录")
    fps = profile["output_fps"]
    camera_bound = profile["max_camera_skew_ms"] / 1000
    joint_bound = profile["max_action_age_ms"] / 1000
    if (
        type(fps) is not int
        or not 1 <= fps <= 240
        or not 0 < camera_bound <= 1
        or not 0 < joint_bound <= 1
    ):
        raise DomainError("不合法的对齐策略")
    if (
        [x["id"] for x in profile["cameras"]] != ["left", "front", "right"]
        or profile["state_streams"] != ["puppetLeft", "puppetRight"]
        or profile["action_streams"] != ["masterLeft", "masterRight"]
    ):
        raise DomainError(
            "本版 AgileX normalizer 仅支持已审查的三相机主从臂布局；其他机器人需新 source adapter"
        )
    read_package_metadata(raw)
    joint_names = profile.get("joint_names")
    dimensions_per_role = (
        len(joint_names) // 2 if isinstance(joint_names, list) and joint_names else None
    )
    camera_files = []
    joint_files = []
    with h5py.File(raw, "r") as source:
        if source.attrs.get("schema") != AGILEX_HDF5_SCHEMA:
            raise EpisodeValidationError("HDF5 原始包 schema 不兼容")
        streams = {}
        for camera in profile["cameras"]:
            camera_id = camera["id"]
            key = f"streams/camera/{camera_id}"
            group = source[key]
            times = np.asarray(group["timestamps"], dtype=np.float64)
            width = int(group.attrs["width"])
            height = int(group.attrs["height"])
            if len(times) != len(group["jpeg"]):
                raise EpisodeValidationError(f"HDF5 相机流长度不一致：{camera_id}")
            camera_files.append((camera_id, width, height))
            streams[f"camera/color/{camera_id}"] = {
                "times": times,
                "group": key,
                "camera": True,
                "index": camera_id,
            }
        for role in profile["state_streams"] + profile["action_streams"]:
            key = f"streams/arm/jointState/{role}"
            group = source[key]
            times = np.asarray(group["timestamps"], dtype=np.float64)
            positions = np.asarray(group["position"], dtype=np.float32)
            if positions.ndim != 2 or positions.shape[0] != len(times):
                raise EpisodeValidationError(f"HDF5 关节流维度不一致：{role}")
            if not np.isfinite(positions).all():
                raise EpisodeValidationError(f"HDF5 关节流包含非有限值：{role}")
            if dimensions_per_role is None:
                dimensions_per_role = positions.shape[1]
            if positions.shape[1] != dimensions_per_role:
                raise EpisodeValidationError(f"HDF5 各机械臂关节维度不一致：{role}")
            joint_files.append(role)
            streams[f"arm/jointState/{role}"] = {
                "times": times,
                "positions": positions,
                "camera": False,
                "index": role,
            }
        for label, stream in streams.items():
            times = stream["times"]
            if (
                times.ndim != 1
                or not len(times)
                or not np.isfinite(times).all()
                or np.any(np.diff(times) <= 0)
            ):
                raise EpisodeValidationError(f"HDF5 时间序列无效：{label}")
        if dimensions_per_role is None:
            raise EpisodeValidationError("HDF5 采集包缺少关节流")
        start = max(float(stream["times"][0]) for stream in streams.values())
        stop = min(float(stream["times"][-1]) for stream in streams.values())
        n = math.floor((stop - start) * fps + 1e-5) + 1
        if n < profile["minimum_frames"] or n > 10_000_000:
            raise EpisodeValidationError(f"有效公共区间过短/过大：{n} 帧")
        dest.parent.mkdir(parents=True, exist_ok=True)
        temporary = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
        temporary.unlink(missing_ok=True)
        report = {
            "fps": fps,
            "frames": n,
            "source_start": start,
            "source_end": stop,
            "duration": n / fps,
            "streams": {},
            "method": "HDF5 streaming; uniform grid; bounded nearest camera; causal bounded joint hold",
            "errors": [],
        }
        try:
            with h5py.File(temporary, "w") as output:
                output.attrs["schema"] = AGILEX_ALIGNED_SCHEMA
                output.attrs["fps"] = fps
                output.attrs["frame_count"] = n
                output.attrs["source_format"] = "agilex_joint_camera_hdf5_v1"
                state = output.create_dataset(
                    "observation.state",
                    shape=(n, dimensions_per_role * 2),
                    dtype=np.float32,
                    compression="gzip",
                    shuffle=True,
                )
                action = output.create_dataset(
                    "action",
                    shape=(n, dimensions_per_role * 2),
                    dtype=np.float32,
                    compression="gzip",
                    shuffle=True,
                )
                grid = start + np.arange(n, dtype=np.float64) / fps
                for label, stream in streams.items():
                    times = stream["times"]
                    if stream["camera"]:
                        right = np.minimum(np.searchsorted(times, grid), len(times) - 1)
                        left = np.maximum(right - 1, 0)
                        indices = np.where(
                            abs(times[left] - grid) <= abs(times[right] - grid),
                            left,
                            right,
                        )
                        offsets = abs(times[indices] - grid)
                        bound = camera_bound
                    else:
                        indices = np.searchsorted(times, grid, side="right") - 1
                        if np.any(indices < 0):
                            raise EpisodeValidationError("没有因果前置关节样本")
                        offsets = grid - times[indices]
                        bound = joint_bound
                    invalid = np.flatnonzero(offsets > bound)
                    if len(invalid):
                        i = int(invalid[0])
                        raise EpisodeValidationError(
                            f"时间对齐越界：{label} frame={i} "
                            f"error={offsets[i] * 1000:.3f}ms limit={bound * 1000:.3f}ms"
                        )
                    stream["indices"] = indices
                    shape = None
                    if stream["camera"]:
                        from .providers.hdf5_images import inspect_images, write_images

                        expected_dimensions = next(
                            (width, height)
                            for camera_id, width, height in camera_files
                            if camera_id == stream["index"]
                        )

                        def inspect(encoded):
                            data = (
                                encoded.tobytes()
                                if isinstance(encoded, np.ndarray)
                                else bytes(encoded)
                            )
                            try:
                                with Image.open(io.BytesIO(data)) as image:
                                    image.load()
                                    if image.mode != "RGB":
                                        raise EpisodeValidationError(
                                            f"相机颜色模式不一致：{label}"
                                        )
                                    size = image.size
                            except DomainError:
                                raise
                            except (OSError, ValueError) as exc:
                                raise EpisodeValidationError(
                                    f"相机图像损坏：{label}"
                                ) from exc
                            if size != expected_dimensions:
                                raise EpisodeValidationError(
                                    f"相机分辨率与 HDF5 元数据不一致：{label}"
                                )
                            return size

                        # Decode every selected frame, reading encoded bytes in
                        # bounded batches. Never pass an HDF5 handle to threads.
                        # A repeated output frame refers to identical encoded
                        # bytes. Check every selected source frame once.
                        for size in inspect_images(
                            source[stream["group"]]["jpeg"], inspect, np.unique(indices)
                        ):
                            shape = size
                        dataset = write_images(
                            output,
                            f"observation.images.{stream['index']}",
                            source[stream["group"]]["jpeg"],
                            indices,
                        )
                        dataset.attrs.update(
                            width=shape[0], height=shape[1], encoded_format="jpeg"
                        )
                    offsets = np.sort(offsets * 1000)
                    report["streams"][label] = {
                        "input_frames": len(times),
                        "selected_unique": len(np.unique(indices)),
                        "repeated_frames": int(np.count_nonzero(np.diff(indices) == 0)),
                        "max_offset_ms": float(offsets[-1]),
                        "p95_offset_ms": float(
                            offsets[min(len(offsets) - 1, int(0.95 * len(offsets)))]
                        ),
                        "dimensions": shape,
                    }

                # Fill each arm independently so left/right joint order is explicit.
                for role_index, key in enumerate(profile["state_streams"]):
                    values = streams[f"arm/jointState/{key}"]["positions"]
                    begin = role_index * dimensions_per_role
                    end = begin + dimensions_per_role
                    state[:, begin:end] = values[
                        streams[f"arm/jointState/{key}"]["indices"]
                    ]
                for role_index, key in enumerate(profile["action_streams"]):
                    values = streams[f"arm/jointState/{key}"]["positions"]
                    begin = role_index * dimensions_per_role
                    end = begin + dimensions_per_role
                    action[:, begin:end] = values[
                        streams[f"arm/jointState/{key}"]["indices"]
                    ]

                package_metadata = read_package_metadata(raw)
                output.create_dataset(
                    "metadata",
                    data=json.dumps(
                        {
                            "schema": AGILEX_ALIGNED_SCHEMA,
                            "source": package_metadata,
                            "profile_id": profile.get("id"),
                            "robot_type": profile.get("robot_type"),
                            "instruction": package_metadata.get("instruction", ""),
                        },
                        ensure_ascii=False,
                    ),
                    dtype=h5py.string_dtype(encoding="utf-8"),
                )
                output.flush()
            with temporary.open("rb") as file:
                os.fsync(file.fileno())
            os.replace(temporary, dest)
            fsync_dir(dest.parent)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    return report
