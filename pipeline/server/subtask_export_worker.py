"""Add a per-frame ``subtask_index`` to a copy of a LeRobot dataset.

Runs inside the pinned LeTools Python environment because only that toolchain
has pyarrow / letools.  The Server never imports this module: it writes a spec
JSON, starts the worker as a subprocess and reads the JSON this script prints.

Contract:

* the source dataset is never modified - a reflink-or-copy mirror is written
  first, or a v2.1 source is converted to v3.0 by LeTools;
* every frame gets exactly one label, ``-1`` when no annotated step covers it,
  so nothing is invented for the unlabelled remainder;
* the vocabulary is written to ``meta/subtasks.parquet`` and declared in
  ``meta/info.json`` so generic LeRobot loaders can map index -> text;
* the finished directory is validated with LeTools before this script succeeds.

Labels are resolved by ``(episode_index, frame_index)``; Parquet timestamps are
not compared, because they are stored as float32 and the frame boundary was
already resolved to whole frames by ``subtask_export.build_plan``.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

BATCH_ROWS = 262144


def _fail(message: str) -> None:
    print(f"Subtask 导出失败：{message}", file=sys.stderr)
    raise SystemExit(1)


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} 必须是 JSON 对象")
    return value


def _write_json(path: Path, value: dict) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _copy_tree(source: Path, destination: Path) -> None:
    """Mirror a dataset with reflinks when the filesystem supports them."""

    from letools._native import clone_or_copy_files

    files = [
        (path, destination / path.relative_to(source))
        for path in sorted(source.rglob("*"))
        if path.is_file()
    ]
    if not files:
        raise ValueError("来源数据集没有可复制的文件")
    clone_or_copy_files(files, max(1, min(8, os.cpu_count() or 1)))


def _prepare(
    source: Path, destination: Path, source_format: str, target_format: str
) -> bool:
    """Mirror an already-v3.0 source, or let LeTools convert an older one."""

    if source_format == target_format:
        _copy_tree(source, destination)
        return False
    from letools import convert

    convert(source, destination, target_format)
    return True


def _lookups(episodes: list[dict]) -> dict:
    """episode_index -> (starts, stops, subtask_index) int64 arrays."""

    import numpy as np

    rows = {}
    for item in episodes:
        segments = item.get("segments") or []
        if not segments:
            continue
        rows[int(item["episode_index"])] = (
            np.array([int(segment[0]) for segment in segments], dtype=np.int64),
            np.array([int(segment[1]) for segment in segments], dtype=np.int64),
            np.array([int(segment[2]) for segment in segments], dtype=np.int64),
        )
    return rows


def _labels(episodes, frames, lookups: dict, unannotated: int):
    """Vectorized lookup; uncovered frames keep ``unannotated``."""

    import numpy as np

    values = np.full(episodes.shape, unannotated, dtype=np.int64)
    for index in np.unique(episodes).tolist():
        lookup = lookups.get(int(index))
        if lookup is None:
            continue
        starts, stops, subtasks = lookup
        mask = episodes == index
        frame_index = frames[mask]
        position = np.searchsorted(starts, frame_index, side="right") - 1
        chosen = np.maximum(position, 0)
        inside = (position >= 0) & (frame_index < stops[chosen])
        assigned = np.full(frame_index.shape, unannotated, dtype=np.int64)
        if inside.any():
            assigned[inside] = subtasks[chosen[inside]]
        values[mask] = assigned
    return values


def _compression(path: Path) -> str:
    import pyarrow.parquet as pq

    metadata = pq.ParquetFile(path).metadata
    if metadata.num_row_groups == 0 or metadata.num_columns == 0:
        return "snappy"
    return (metadata.row_group(0).column(0).compression or "snappy").lower()


def _rewrite_shard(path: Path, lookups: dict, unannotated: int, observed: dict) -> int:
    """Append ``subtask_index`` to one shard, keeping row order and dtypes."""

    import pyarrow as pa
    import pyarrow.parquet as pq

    reader = pq.ParquetFile(path)
    schema = reader.schema_arrow
    names = set(schema.names)
    missing = {"episode_index", "frame_index"} - names
    if missing:
        raise ValueError(f"{path.name} 缺少列：{'、'.join(sorted(missing))}")
    fields = [field for field in schema if field.name != "subtask_index"]
    output_schema = pa.schema(
        [*fields, pa.field("subtask_index", pa.int64(), nullable=True)],
        metadata=schema.metadata,
    )
    compression = _compression(path)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    labelled = 0
    try:
        with pq.ParquetWriter(
            tmp, output_schema, compression=compression, write_statistics=True
        ) as writer:
            for batch in reader.iter_batches(batch_size=BATCH_ROWS):
                episodes = batch.column("episode_index").to_numpy(zero_copy_only=False)
                frames = batch.column("frame_index").to_numpy(zero_copy_only=False)
                labels = _labels(episodes, frames, lookups, unannotated)
                _observe(observed, episodes, labels, unannotated)
                labelled += int((labels != unannotated).sum())
                table = pa.Table.from_batches([batch], schema=batch.schema)
                if "subtask_index" in table.column_names:
                    table = table.drop_columns(["subtask_index"])
                table = table.append_column(
                    "subtask_index",
                    pa.array(labels, type=pa.int64(), from_pandas=False),
                )
                writer.write_table(
                    table.cast(output_schema).replace_schema_metadata(schema.metadata)
                )
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return labelled


def _observe(observed: dict, episodes, labels, unannotated: int) -> None:
    """Per-episode frame totals and a dataset-wide label histogram.

    ``observed["episodes"]`` counts frames per episode, ``observed["labels"]``
    counts frames per subtask index.  Nothing is keyed by frame, so a shard is
    summarized from the same arrays that were just written.
    """

    import numpy as np

    per_episode = observed.setdefault("episodes", {})
    uniques, totals = np.unique(episodes, return_counts=True)
    for index, total in zip(uniques.tolist(), totals.tolist()):
        per_episode[int(index)] = per_episode.get(int(index), 0) + int(total)
    histogram = observed.setdefault("labels", {})
    values, counts = np.unique(labels, return_counts=True)
    for value, count in zip(values.tolist(), counts.tolist()):
        histogram[int(value)] = histogram.get(int(value), 0) + int(count)


def _write_subtasks(path: Path, vocabulary: list[dict], unannotated: int, text: str):
    """``subtask`` as the index column, mirroring ``meta/tasks.parquet``."""

    rows = [
        {"subtask_index": int(item["subtask_index"]), "subtask": str(item["subtask"])}
        for item in vocabulary
    ]
    rows.append({"subtask_index": int(unannotated), "subtask": text})
    rows.sort(key=lambda row: row["subtask_index"])
    try:
        import pandas as pd
    except ImportError:  # pragma: no cover - letools ships pandas
        import pyarrow as pa
        import pyarrow.parquet as pq

        pq.write_table(pa.Table.from_pylist(rows), path, compression="snappy")
        return
    frame = pd.DataFrame(rows, columns=["subtask", "subtask_index"]).set_index(
        "subtask"
    )
    frame.to_parquet(path, engine="pyarrow", compression="snappy")


def _subtask_stats(counts: dict, total: int) -> dict:
    """min/max/mean/std of the label column from its integer histogram."""

    if not counts or not total:
        return {}
    ordered = sorted(counts.items())
    low, high = ordered[0][0], ordered[-1][0]
    mean = sum(value * count for value, count in ordered) / total
    variance = sum(count * (value - mean) ** 2 for value, count in ordered) / total
    return {
        "min": [int(low)],
        "max": [int(high)],
        "mean": [float(mean)],
        "std": [float(variance**0.5)],
        "count": [int(total)],
    }


def patch(destination: Path, spec: dict) -> dict:
    """Write labels, metadata and statistics in place, then validate."""

    info_path = destination / "meta/info.json"
    info = _read_json(info_path)
    if info.get("codebase_version") != spec["target_format"]:
        raise ValueError(
            f"输出数据集版本为 {info.get('codebase_version')}，"
            f"期望 {spec['target_format']}"
        )
    unannotated = int(spec["unannotated_index"])
    lookups = _lookups(spec["episodes"])
    observed: dict = {}
    shards = sorted((destination / "data").rglob("*.parquet"))
    if not shards:
        raise ValueError("输出数据集没有 data/**/*.parquet")

    labelled_frames = 0
    for shard in shards:
        labelled_frames += _rewrite_shard(shard, lookups, unannotated, observed)
    episode_frames = observed.get("episodes", {})
    counts = observed.get("labels", {})

    expected = {
        int(item["episode_index"]): int(item["frames"]) for item in spec["episodes"]
    }
    mismatched = [
        (index, actual, expected[index])
        for index, actual in sorted(episode_frames.items())
        if index in expected and actual != expected[index]
    ]
    if mismatched:
        detail = "、".join(
            f"Episode {index}: 实际 {actual} 帧 / 标注计划 {planned} 帧"
            for index, actual, planned in mismatched[:8]
        )
        raise ValueError("来源帧数与标注计划不一致，逐帧标签会错位：" + detail)

    total_frames = sum(episode_frames.values())
    if info.get("total_frames") not in (None, total_frames):
        raise ValueError(
            f"info.total_frames={info['total_frames']}，实际 {total_frames} 帧"
        )

    features = info.get("features")
    if not isinstance(features, dict):
        raise ValueError("meta/info.json 缺少 features")
    features["subtask_index"] = {"dtype": "int64", "shape": [1], "names": None}
    _write_json(info_path, info)
    # Lossless manual annotations (including arbitrary VQA/tool arguments).
    # Keep the original language columns untouched; this sidecar is authoritative
    # for reviewed episodes and travels with the independent dataset copy.
    manual = {
        str(item["episode_index"]): item["atoms"]
        for item in spec["episodes"]
        if "atoms" in item
    }
    if manual:
        _write_json(
            destination / "meta/bodypipeline-manual-annotations.json",
            {"version": 1, "episodes": manual},
        )
    tasks = {
        str(item["episode_index"]): item["task_document"]
        for item in spec["episodes"]
        if "task_document" in item
    }
    if tasks:
        _write_json(
            destination / "meta/bodypipeline-task-annotations.json",
            {"version": 1, "episodes": tasks},
        )
    else:
        # A fresh AI export is an independent candidate. A copied human-review
        # sidecar describes the old labels; keep it as an archive, not as the
        # candidate's active document. The source itself is never changed.
        previous = destination / "meta/bodypipeline-task-annotations.json"
        if previous.is_file():
            os.replace(
                previous,
                previous.with_name(
                    f"bodypipeline-task-annotations-source-{uuid.uuid4().hex}.json"
                ),
            )

    _write_subtasks(
        destination / "meta/subtasks.parquet",
        spec.get("vocabulary") or [],
        unannotated,
        spec.get("unannotated_text") or "",
    )

    stats = _subtask_stats(counts, total_frames)
    stats_path = destination / "meta/stats.json"
    if stats and stats_path.is_file():
        existing = _read_json(stats_path)
        existing["subtask_index"] = stats
        _write_json(stats_path, existing)

    from letools.validation import validate_dataset

    report = validate_dataset(destination)
    if not report.valid:
        raise ValueError("结构验证未通过：" + "；".join(report.errors[:8]))

    missing_episodes = sorted(set(expected) - set(episode_frames))
    return {
        "validated": True,
        "episodes": len(episode_frames),
        "frames": total_frames,
        "labelled_episodes": len(expected),
        "labelled_frames": labelled_frames,
        "unannotated_frames": total_frames - labelled_frames,
        "missing_episodes": missing_episodes,
        "shards": len(shards),
        "vocabulary": len(spec.get("vocabulary") or []),
        "warnings": list(report.warnings)[:20],
    }


def main() -> None:
    if len(sys.argv) != 2:
        _fail("用法：subtask_export_worker.py <spec.json>")
    spec = _read_json(Path(sys.argv[1]))
    source = Path(spec["source"])
    destination = Path(spec["destination"])
    if not (source / "meta/info.json").is_file():
        _fail(f"来源不是 LeRobot 数据集：{source}")
    if destination.exists():
        _fail(f"输出目录已存在：{destination}")
    converted = _prepare(
        source, destination, spec["source_format"], spec["target_format"]
    )
    result = patch(destination, spec)
    result["converted"] = converted
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - reported as a non-zero exit
        _fail(f"{type(exc).__name__}: {exc}")
