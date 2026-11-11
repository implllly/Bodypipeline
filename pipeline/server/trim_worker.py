"""Isolated LeTools worker for idle-frame detection and boundary trimming.

Runs inside the pinned LeTools Python environment because only that toolchain
has pyarrow / av / letools. The Server never imports this module: it writes a
spec JSON, starts this script as a subprocess and reads the JSON printed here.

Modes:

``detect``
    Read every episode through the LeTools source plugin and return the frame
    range that would be kept. The source dataset is opened read-only.

``detect-static``
    The whole-recording counterpart: return every kept range per episode after
    removing sustained static intervals.

``trim``
    Write a new dataset into ``destination``: numeric rows are sliced, every
    camera video is cut to the same frame ranges, and indices, timestamps,
    per-episode statistics and dataset metadata are rebuilt. When the plan
    contains several ``keep_ranges`` for one source episode, the kept ranges are
    joined in order into a single output episode; a source episode is only
    dropped when its plan has no ranges at all. The source dataset is never
    modified.

``trim-manual``
    Same writer as ``trim``, but the plan is derived from frame ranges the user
    selected for one episode. The removed ranges are subtracted from that
    episode's real length and the remaining frames are joined back together.
    The source dataset is never modified.

Both detection modes and ``trim`` use the same detection modules so a preview,
a standalone trim and an in-conversion trim cannot disagree.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

# The pinned LeTools environment does not install this package, so the worker
# adds the repository root before importing the shared detection module. When
# the package is installed, this path is inside site-packages and already on
# ``sys.path``.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# H.264 encoding of one clip scales poorly beyond a few cores, so more than
# this many independent cuts only add I/O contention and process memory.
MAX_VIDEO_WORKERS = 32


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} 必须是 JSON 对象")
    return value


def _feature_matrix(table, feature: str, dimensions: int):
    """Return one episode as an ``(frames, dimensions)`` float64 matrix."""

    import numpy as np

    if feature in table.column_names:
        values = np.asarray(table[feature].to_pylist(), dtype=np.float64)
    else:
        prefix = feature + "."
        columns = sorted(
            (name for name in table.column_names if name.startswith(prefix)),
            key=lambda name: int(name.rsplit(".", 1)[-1]),
        )
        if not columns:
            raise ValueError(f"数据分片缺少特征 {feature}")
        values = np.asarray(
            [table.column(name).to_pylist() for name in columns], dtype=np.float64
        ).T
    if values.ndim == 1:
        values = values.reshape(-1, 1)
    else:
        values = values.reshape(values.shape[0], -1)
    if values.shape[1] != dimensions:
        raise ValueError(
            f"特征 {feature} 的维度 {values.shape[1]} 与元数据 {dimensions} 不一致"
        )
    return values


def detect(spec: dict) -> dict:
    from letools.plugins import open_dataset

    from pipeline.server.stationary_trim import detect_dataset

    policy = spec["policy"]
    source = open_dataset(Path(spec["source"]))
    values: dict[int, object] = {}
    frames: dict[int, int] = {}
    for episode in source.episodes:
        table = source.read_episode(episode)
        matrix = _feature_matrix(table, policy["feature"], policy["dimensions"])
        values[episode.index] = matrix
        frames[episode.index] = matrix.shape[0]
    plan = detect_dataset(values, policy)
    plan.update(
        source_episode_count=source.metadata.total_episodes,
        source_frame_count=source.metadata.total_frames,
        source_format=source.metadata.version,
        episode_lengths=frames,
    )
    return plan


def detect_static(spec: dict) -> dict:
    """Detect whole-recording static intervals for the conversion option."""

    from letools.plugins import open_dataset

    from pipeline.server.static_intervals import detect_dataset

    policy = spec["policy"]
    source = open_dataset(Path(spec["source"]))
    values: dict[int, object] = {}
    frames: dict[int, int] = {}
    for episode in source.episodes:
        table = source.read_episode(episode)
        matrix = _feature_matrix(table, policy["feature"], policy["dimensions"])
        values[episode.index] = matrix
        frames[episode.index] = matrix.shape[0]
    plan = detect_dataset(values, policy)
    plan.update(
        source_episode_count=source.metadata.total_episodes,
        source_frame_count=source.metadata.total_frames,
        source_format=source.metadata.version,
        episode_lengths=frames,
    )
    return plan


# --------------------------------------------------------------------- rewrite
NUMERIC_DTYPES = {
    "float32",
    "float64",
    "float16",
    "int64",
    "int32",
    "int16",
    "int8",
    "uint8",
    "uint16",
    "bool",
}


def _write_table(path: Path, table) -> None:
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)  # Never mutate a hard-linked source file.
    pq.write_table(table, path)


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def _scalar_stats(values) -> dict:
    import numpy as np

    data = np.asarray(values, dtype=np.float64)
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    return {
        "min": data.min(axis=0).tolist(),
        "max": data.max(axis=0).tolist(),
        "mean": data.mean(axis=0).tolist(),
        "std": data.std(axis=0).tolist(),
        "count": [int(data.shape[0])] * data.shape[1],
    }


def _feature_stats(table, feature: str) -> dict:
    import numpy as np

    data = np.asarray(table[feature].to_pylist(), dtype=np.float64)
    if data.ndim == 1:
        data = data.reshape(-1, 1)
    else:
        data = data.reshape(data.shape[0], -1)
    stats = {
        "min": data.min(axis=0).tolist(),
        "max": data.max(axis=0).tolist(),
        "mean": data.mean(axis=0).tolist(),
        "std": data.std(axis=0).tolist(),
        "count": [int(data.shape[0])] * data.shape[1],
    }
    for quantile, key in ((1, "q01"), (10, "q10"), (50, "q50"), (90, "q90"), (99, "q99")):
        stats[key] = np.percentile(data, quantile, axis=0).tolist()
    return stats


def _episode_stats(table, features: dict, original: dict) -> dict:
    stats: dict[str, dict] = {}
    for name, spec in features.items():
        if name not in table.column_names:
            continue
        if spec.get("dtype") not in NUMERIC_DTYPES:
            # Video/image statistics require decoding frames per episode; the
            # source moments stay valid enough for a read-only quality report.
            if name in original:
                stats[name] = copy.deepcopy(original[name])
            continue
        try:
            stats[name] = _feature_stats(table, name)
        except (TypeError, ValueError):
            if name in original:
                stats[name] = copy.deepcopy(original[name])
    return stats


def _copy_ancillary(source: Path, destination: Path) -> None:
    from letools._native import clone_or_copy_files

    files = [
        (path, destination / path.relative_to(source))
        for path in sorted(source.rglob("*"))
        if path.is_file()
        and path.relative_to(source).parts[0] not in {"data", "videos"}
    ]
    if files:
        clone_or_copy_files(files, max(1, min(8, os.cpu_count() or 1)))


def _original_rows(source, version: str) -> dict[int, dict]:
    import pyarrow.parquet as pq

    rows: dict[int, dict] = {}
    if version == "v3.0":
        for path in sorted((source.root / "meta/episodes").rglob("*.parquet")):
            for row in pq.read_table(path).to_pylist():
                rows[int(row["episode_index"])] = row
    elif version == "v2.1":
        path = source.root / "meta/episodes.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rows[int(row["episode_index"])] = row
    else:
        raise ValueError(f"不支持的 LeRobot 版本：{version}")
    return rows


def _trim_video(source_video: Path, output: Path, ranges, fps: float, threads: int | None = None) -> None:
    from fractions import Fraction

    from pipeline.server.recycle_worker import _trim_video as trim

    # ``av.add_stream(rate=...)`` needs an int/Fraction, not a float.
    trim(
        source_video,
        output,
        ranges,
        Fraction(fps).limit_denominator(1_000_000),
        threads=threads,
    )


def _trim_video_task(task: tuple) -> None:
    """Cut one independent (source video -> output video) pair.

    Runs in a worker process so files belonging to different cameras and source
    shards are encoded concurrently. Each task opens its own container and writes
    a path decided by the parent, so no mutable state is shared.
    """

    source, output, ranges, fps, threads = task
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    _trim_video(Path(source), output, ranges, fps, threads=threads)


def _available_cpus() -> int:
    """CPUs this worker may use, inside or outside a Slurm step."""

    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 1
    requested = os.environ.get("SLURM_CPUS_PER_TASK", "")
    if requested.isdigit() and int(requested) > 0:
        cpus = min(cpus, int(requested))
    return max(1, cpus)


def video_parallelism(spec: dict, task_count: int) -> tuple[int, int]:
    """Return ``(workers, threads_per_encoder)`` for the independent cuts.

    The CPU budget comes from the current process, so the same code is bounded
    by a Slurm step's allocation. One encoder thread per worker is the default:
    H.264 encoders scale better across independent videos than inside one clip.
    """

    if task_count < 1:
        return 0, 1
    configured = spec.get("video_workers")
    if configured is not None:
        if (
            isinstance(configured, bool)
            or not isinstance(configured, int)
            or configured < 1
        ):
            raise ValueError("video_workers 必须是正整数")
        workers = configured
    else:
        workers = min(MAX_VIDEO_WORKERS, _available_cpus())
    workers = max(1, min(task_count, workers))
    return workers, max(1, _available_cpus() // workers)


def _plan_ranges(item: dict, length: int) -> list[tuple[int, int]]:
    """Return the kept frame ranges for one source episode.

    A plan row may describe a single boundary trim (``keep_start`` /
    ``keep_stop``) or several ranges (``keep_ranges``). Adjacent ranges are
    merged so the written data never contains a zero-frame gap.
    """

    if item.get("status") == "removed":
        return []
    raw = item.get("keep_ranges")
    if raw is None:
        raw = [[item.get("keep_start", 0), item.get("keep_stop", length)]]
    ranges: list[tuple[int, int]] = []
    for value in raw:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError("保留区间必须是 [start, stop] 数组")
        start, stop = int(value[0]), int(value[1])
        if not 0 <= start < stop <= length:
            raise ValueError("保留区间超出 Episode 帧范围")
        if ranges and start < ranges[-1][1]:
            raise ValueError("保留区间重叠或未排序")
        if ranges and start == ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], stop)
        else:
            ranges.append((start, stop))
    return ranges


def trim(spec: dict) -> dict:
    import numpy as np
    import pyarrow as pa
    from letools._stats import aggregate_episode_stats, flatten_stats
    from letools.plugins import open_dataset
    from letools.validation import validate_dataset

    root = Path(spec["source"])
    destination = Path(spec["destination"])
    plan = {int(item["episode_index"]): item for item in spec["episodes"]}
    fps = float(spec["fps"])
    if destination.exists():
        raise ValueError("输出目录已存在，拒绝覆盖")
    source = open_dataset(root)
    info = copy.deepcopy(source.metadata.info)
    version = source.metadata.version
    chunks_size = int(info.get("chunks_size", 1000))
    features = info.get("features") or {}
    # Each source episode becomes at most one output episode. A plan may carry
    # several kept ranges (static intervals removed inside the episode); those
    # ranges are joined in order instead of being published as separate
    # episodes, so the output keeps the source episode identity.
    outputs: list[dict] = []
    mapping: dict[str, int] = {}
    for episode in source.episodes:
        item = plan.get(episode.index)
        if item is None:
            raise ValueError(f"计划缺少 Episode {episode.index} 的裁剪范围")
        ranges = _plan_ranges(item, episode.length)
        if not ranges:
            continue
        new_id = len(outputs)
        outputs.append(
            {
                "source_index": episode.index,
                "ranges": ranges,
                "new_id": new_id,
            }
        )
        mapping[str(episode.index)] = new_id
    if not outputs:
        raise ValueError("裁剪会删除全部 Episode；请关闭“删除整段静止 Episode”")
    by_source = {episode.index: episode for episode in source.episodes}

    _copy_ancillary(root, destination)
    original_rows = _original_rows(source, version)

    episodes_dir = destination / "meta/episodes"
    data_files: list[Path] = []
    rows: list[dict] = []
    moments: list[dict] = []
    stats_rows: list[dict] = []
    details: list[dict] = []
    global_offset = 0
    for output in outputs:
        episode = by_source[output["source_index"]]
        new_id = output["new_id"]
        ranges = output["ranges"]
        episode_table = source.read_episode(episode)
        pieces = [
            episode_table.slice(start, stop - start) for start, stop in ranges
        ]
        table = pieces[0] if len(pieces) == 1 else pa.concat_tables(pieces)
        length = table.num_rows
        for name, values in {
            "episode_index": np.full(length, new_id, dtype=np.int64),
            "index": np.arange(global_offset, global_offset + length, dtype=np.int64),
            "frame_index": np.arange(length, dtype=np.int64),
            "timestamp": (np.arange(length) / fps).astype(np.float32),
        }.items():
            if name in table.column_names:
                position = table.schema.get_field_index(name)
                field = table.schema.field(name)
                table = table.set_column(
                    position, field, pa.array(values, type=field.type)
                )
        already = {name for name in table.column_names}
        episode_stats = _episode_stats(table, features, episode.stats)
        for name in ("episode_index", "index", "frame_index"):
            if name in already:
                episode_stats[name] = _scalar_stats(table[name].to_numpy())
        moments.append(episode_stats)
        if version == "v2.1":
            stats_rows.append({"episode_index": new_id, "stats": episode_stats})
        row = {
            key: value
            for key, value in original_rows[episode.index].items()
            if not key.startswith("stats/")
        }
        row.update(
            episode_index=new_id,
            tasks=list(episode.tasks),
            length=length,
        )
        if version == "v3.0":
            chunk, file = divmod(new_id, chunks_size)
            data_path = info["data_path"].format(chunk_index=chunk, file_index=file)
            row.update(
                {
                    "data/chunk_index": chunk,
                    "data/file_index": file,
                    "dataset_from_index": global_offset,
                    "dataset_to_index": global_offset + length,
                    "meta/episodes/chunk_index": 0,
                    "meta/episodes/file_index": 0,
                    **flatten_stats(episode_stats),
                }
            )
        else:
            data_path = info["data_path"].format(
                episode_index=new_id, episode_chunk=new_id // chunks_size
            )
        _write_table(destination / data_path, table)
        data_files.append(destination / data_path)
        rows.append(row)
        details.append(
            {
                "episode_index": new_id,
                "source_episode_index": episode.index,
                "keep_ranges": [[start, stop] for start, stop in ranges],
                "length": length,
                "source_length": episode.length,
                "removed_frames": episode.length - length,
            }
        )
        global_offset += length

    # ------------------------------------------------------------------ videos
    # Every (source video -> output video) pair is independent: they never read
    # or write the same path, and the output names are decided here in the
    # parent. Encode them concurrently inside the CPU budget, then update the
    # per-episode timestamp columns in the original order.
    video_jobs: list[dict] = []
    for camera in source.metadata.video_keys:
        groups: dict[Path, list[dict]] = defaultdict(list)
        for output in outputs:
            episode = by_source[output["source_index"]]
            video = episode.videos.get(camera)
            if video is None:
                raise ValueError(f"Episode {episode.index} 缺少 {camera} 视频")
            groups[video.path].append(
                {
                    "episode": episode,
                    "video": video,
                    "new_id": output["new_id"],
                    "ranges": [
                        (
                            video.start + start / fps,
                            video.start + stop / fps,
                        )
                        for start, stop in output["ranges"]
                    ],
                }
            )
        for number, (path, items) in enumerate(groups.items()):
            if version == "v2.1":
                for item in items:
                    new_id = item["new_id"]
                    video_jobs.append(
                        {
                            "source": path,
                            "output": destination
                            / info["video_path"].format(
                                video_key=camera,
                                episode_index=new_id,
                                episode_chunk=new_id // chunks_size,
                            ),
                            "ranges": item["ranges"],
                        }
                    )
                continue
            merged: list[tuple[float, float]] = []
            for item in items:
                for start, stop in item["ranges"]:
                    if merged and abs(start - merged[-1][1]) < 1e-6:
                        merged[-1] = (merged[-1][0], stop)
                    else:
                        merged.append((start, stop))
            chunk, file = divmod(number, chunks_size)
            video_jobs.append(
                {
                    "source": path,
                    "output": destination
                    / info["video_path"].format(
                        video_key=camera, chunk_index=chunk, file_index=file
                    ),
                    "ranges": merged,
                    "camera": camera,
                    "chunk": chunk,
                    "file": file,
                    "items": items,
                }
            )

    workers, threads = video_parallelism(spec, len(video_jobs))
    tasks = [
        (
            str(job["source"]),
            str(job["output"]),
            job["ranges"],
            fps,
            threads,
        )
        for job in video_jobs
    ]
    if workers <= 1:
        for task in tasks:
            _trim_video_task(task)
    else:
        # ``map`` keeps submission order and re-raises the first worker failure.
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for _ in pool.map(_trim_video_task, tasks, chunksize=1):
                pass

    for job in video_jobs:
        if version == "v2.1":
            continue
        camera = job["camera"]
        elapsed = 0.0
        prefix = f"videos/{camera}"
        for item in job["items"]:
            new_id = item["new_id"]
            row = rows[new_id]
            duration = sum(stop - start for start, stop in item["ranges"])
            row.update(
                {
                    f"{prefix}/chunk_index": job["chunk"],
                    f"{prefix}/file_index": job["file"],
                    f"{prefix}/from_timestamp": elapsed,
                    f"{prefix}/to_timestamp": elapsed + duration,
                }
            )
            elapsed += duration

    # -------------------------------------------------------------- metadata
    rows.sort(key=lambda row: row["episode_index"])
    if version == "v3.0":
        shutil.rmtree(episodes_dir, ignore_errors=True)
        _write_table(
            episodes_dir / "chunk-000/file-000.parquet",
            pa.Table.from_pylist(rows),
        )
    else:
        _jsonl(destination / "meta/episodes.jsonl", rows)
        _jsonl(destination / "meta/episodes_stats.jsonl", stats_rows)
    splits = {}
    for name, bounds in info.get("splits", {"train": f"0:{source.metadata.total_episodes}"}).items():
        start, end = (int(value) for value in bounds.split(":"))
        splits[name] = (
            f"{sum(output['source_index'] < start for output in outputs)}:"
            f"{sum(output['source_index'] < end for output in outputs)}"
        )
    info.update(
        total_episodes=len(outputs),
        total_frames=global_offset,
        splits=splits,
        bodypipeline_revision=str(time.time_ns()),
    )
    if version == "v2.1":
        info.update(
            total_chunks=(len(outputs) + chunks_size - 1) // chunks_size,
            total_videos=len(outputs) * len(source.metadata.video_keys),
        )
    _write_metadata(destination / "meta/info.json", info)
    common = (
        set.intersection(*[set(stats) for stats in moments]) if moments else set()
    )
    _write_metadata(
        destination / "meta/stats.json",
        aggregate_episode_stats(
            [{key: value for key, value in stats.items() if key in common} for stats in moments]
        )
        if moments
        else {},
    )

    report = validate_dataset(destination, deep=True)
    if not report.valid:
        raise ValueError("裁剪后数据集校验失败：" + "; ".join(report.errors[:5]))
    checked = open_dataset(destination)
    offset = 0
    for episode in checked.episodes:
        table = checked.read_episode(episode)
        if table.num_rows != episode.length:
            raise ValueError(f"Episode {episode.index} 行数与元数据不一致")
        if "frame_index" in table.column_names and table["frame_index"].to_pylist() != list(
            range(episode.length)
        ):
            raise ValueError(f"Episode {episode.index} 的 frame_index 不连续")
        if "index" in table.column_names and table["index"].to_pylist() != list(
            range(offset, offset + episode.length)
        ):
            raise ValueError("裁剪后全局帧索引不连续")
        if "timestamp" in table.column_names:
            expected = [frame / fps for frame in range(episode.length)]
            actual = table["timestamp"].to_pylist()
            if len(actual) != len(expected) or any(
                abs(left - right) > 1e-4
                for left, right in zip(actual, expected, strict=True)
            ):
                raise ValueError(f"Episode {episode.index} 的时间戳没有重建")
        offset += episode.length
    return {
        "validated": True,
        "version": version,
        "episodes": len(outputs),
        "source_episodes": source.metadata.total_episodes,
        "frames": global_offset,
        "frames_before": source.metadata.total_frames,
        "frames_removed": source.metadata.total_frames - global_offset,
        "mapping": mapping,
        "episodes_detail": details,
        "video_jobs": len(video_jobs),
        "video_workers": workers,
        "video_threads": threads,
    }


def _normalize_cuts(raw, length: int) -> list[tuple[int, int]]:
    """Validate manually entered frame ranges and merge touching/overlapping ones."""

    if not isinstance(raw, list) or not raw:
        raise ValueError("至少需要一个删除区间")
    if len(raw) > 100:
        raise ValueError("删除区间不能超过 100 段")
    cuts: list[tuple[int, int]] = []
    for value in raw:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError("删除区间必须是 [start, stop] 两元数组")
        start, stop = value
        for item in (start, stop):
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError("删除区间必须使用整数帧号")
        if not 0 <= start < stop <= length:
            raise ValueError(
                f"删除区间 [{start}, {stop}) 超出 Episode 范围 [0, {length})"
            )
        cuts.append((start, stop))
    cuts.sort()
    merged: list[tuple[int, int]] = []
    for start, stop in cuts:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
        else:
            merged.append((start, stop))
    return merged


def manual_plan(spec: dict) -> list[dict]:
    """Build a join plan from manually removed frame ranges.

    The plan keeps the source episode count: the target episode is written as
    the complement of the removed ranges, so its remaining frames are joined
    into one continuous output episode.
    """

    from letools.plugins import open_dataset

    index = spec.get("episode_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("episode_index 必须是非负整数")
    source = open_dataset(Path(spec["source"]))
    episode = next((item for item in source.episodes if item.index == index), None)
    if episode is None:
        raise ValueError(f"找不到 Episode {index}")
    length = episode.length
    cuts = _normalize_cuts(spec.get("remove_ranges"), length)
    keep: list[list[int]] = []
    cursor = 0
    for start, stop in cuts:
        if cursor < start:
            keep.append([cursor, start])
        cursor = stop
    if cursor < length:
        keep.append([cursor, length])
    if not keep:
        raise ValueError("删除后会得到空 Episode；请至少保留一帧")
    if keep == [[0, length]]:
        raise ValueError("没有需要删除的时间段")
    kept = sum(stop - start for start, stop in keep)
    rows = []
    for item in source.episodes:
        if item.index == index:
            rows.append(
                {
                    "episode_index": item.index,
                    "frames": item.length,
                    "keep_ranges": keep,
                    "keep_frames": kept,
                    "removed_frames": item.length - kept,
                    "status": "trimmed",
                    "reason": f"手动删除 {len(cuts)} 段时间，保留片段前后直接相连",
                }
            )
        else:
            rows.append(
                {
                    "episode_index": item.index,
                    "frames": item.length,
                    "keep_ranges": [[0, item.length]],
                    "keep_frames": item.length,
                    "removed_frames": 0,
                    "status": "unchanged",
                    "reason": "未修改",
                }
            )
    return rows


def trim_manual(spec: dict) -> dict:
    """Remove manually selected ranges and join the remaining frames."""

    rows = manual_plan(spec)
    return trim({**spec, "mode": "trim", "episodes": rows})


def _write_metadata(path: Path, value: dict) -> None:
    from pipeline.shared.core import atomic_json

    atomic_json(path, value)


def run(spec_path: str) -> dict:
    spec = _read_json(Path(spec_path))
    mode = spec.get("mode")
    if mode == "detect":
        return detect(spec)
    if mode == "detect-static":
        return detect_static(spec)
    if mode == "trim":
        return trim(spec)
    if mode == "trim-manual":
        return trim_manual(spec)
    raise ValueError("未知的裁剪任务模式")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("spec")
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                run(args.spec), ensure_ascii=False, allow_nan=False, default=list
            )
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the Server as job error
        print(f"静止帧裁剪失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
