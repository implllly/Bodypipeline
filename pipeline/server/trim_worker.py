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
    per-episode statistics and dataset metadata are rebuilt. One source episode
    may produce several output episodes when the plan contains ``keep_ranges``.
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
from pathlib import Path

# The pinned LeTools environment does not install this package, so the worker
# adds the repository root before importing the shared detection module. When
# the package is installed, this path is inside site-packages and already on
# ``sys.path``.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


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


def _trim_video(source_video: Path, output: Path, ranges, fps: float) -> None:
    from fractions import Fraction

    from pipeline.server.recycle_worker import _trim_video as trim

    # ``av.add_stream(rate=...)`` needs an int/Fraction, not a float.
    trim(source_video, output, ranges, Fraction(fps).limit_denominator(1_000_000))


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
    # One source episode may become several output episodes after a static
    # interval is removed. ``outputs`` is the flattened, order-preserving plan.
    outputs: list[dict] = []
    mapping: dict[str, list[int]] = {}
    for episode in source.episodes:
        item = plan.get(episode.index)
        if item is None:
            raise ValueError(f"计划缺少 Episode {episode.index} 的裁剪范围")
        for range_index, (start, stop) in enumerate(
            _plan_ranges(item, episode.length)
        ):
            new_id = len(outputs)
            outputs.append(
                {
                    "source_index": episode.index,
                    "range_index": range_index,
                    "start": start,
                    "stop": stop,
                    "new_id": new_id,
                }
            )
            mapping.setdefault(str(episode.index), []).append(new_id)
    if not outputs:
        raise ValueError("裁剪会删除全部 Episode；请关闭“删除整段静止 Episode”")
    mapping = {
        key: values[0] if len(values) == 1 else values
        for key, values in mapping.items()
    }
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
        start, stop = output["start"], output["stop"]
        table = source.read_episode(episode).slice(start, stop - start)
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
                "output_range_index": output["range_index"],
                "keep_start": start,
                "keep_stop": stop,
                "length": length,
                "source_length": episode.length,
            }
        )
        global_offset += length

    # ------------------------------------------------------------------ videos
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
                    "start": video.start + output["start"] / fps,
                    "stop": video.start + output["stop"] / fps,
                }
            )
        for number, (path, items) in enumerate(groups.items()):
            if version == "v2.1":
                for item in items:
                    new_id = item["new_id"]
                    output = destination / info["video_path"].format(
                        video_key=camera,
                        episode_index=new_id,
                        episode_chunk=new_id // chunks_size,
                    )
                    output.parent.mkdir(parents=True, exist_ok=True)
                    _trim_video(path, output, [(item["start"], item["stop"])], fps)
                continue
            merged: list[tuple[float, float]] = []
            for item in items:
                start, stop = item["start"], item["stop"]
                if merged and abs(start - merged[-1][1]) < 1e-6:
                    merged[-1] = (merged[-1][0], stop)
                else:
                    merged.append((start, stop))
            chunk, file = divmod(number, chunks_size)
            output = destination / info["video_path"].format(
                video_key=camera, chunk_index=chunk, file_index=file
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            _trim_video(path, output, merged, fps)
            elapsed = 0.0
            prefix = f"videos/{camera}"
            for item in items:
                new_id = item["new_id"]
                row = rows[new_id]
                duration = item["stop"] - item["start"]
                row.update(
                    {
                        f"{prefix}/chunk_index": chunk,
                        f"{prefix}/file_index": file,
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
    }


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
