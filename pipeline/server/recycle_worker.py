"""Transactional Episode removal. Runs in the letools Python environment.

Build and validate a replacement beside the dataset, then publish with Linux
RENAME_EXCHANGE. A persisted marker makes publication/recovery unambiguous.
No path or episode selection is accepted directly from an HTTP request here.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import fcntl
import json
import os
import shutil
import stat
import time
from collections import defaultdict
from pathlib import Path

from pipeline.shared.core import Store, atomic_json, fsync_dir

MARKER = ".bodypipeline-recycle.json"


def _stage(row):
    path = Path(row["dataset_path"])
    return path.with_name(f".{path.name}.recycle-{row['operation']['id']}")


def _published(row):
    try:
        marker = json.loads((Path(row["dataset_path"]) / MARKER).read_text())
        return marker.get("operation") == row["operation"]["id"]
    except (OSError, ValueError):
        return False


def recover_operation(store, row):
    """Finish a published transaction, or discard an unpublished replacement."""
    op = row["operation"]
    stage = _stage(row)
    if _published(row):
        marker = json.loads((Path(row["dataset_path"]) / MARKER).read_text())
        mapping = marker["mapping"]
        entries = {
            str(mapping[key]): {**entry, "episode": mapping[key]}
            for key, entry in op["entries_before"].items()
            if key in mapping
        }
        # Keep the journal until the old payload is actually gone. Retrying after
        # a crash uses the original selection/mapping exactly once.
        if stage.exists():
            shutil.rmtree(stage)
            fsync_dir(stage.parent)
        row["entries"] = entries
        op.update(
            state="completed",
            progress="清理完成，H800 中所选片段已删除",
            finished_at=time.time(),
            remaining_episodes=marker["total_episodes"],
            deleted_frames=marker["deleted_frames"],
            mapping=mapping,
        )
    else:
        if stage.exists():
            shutil.rmtree(stage)
        op.update(
            state="failed",
            progress="清理未完成，原数据保持完整",
            error=op.get("error") or "清理进程中断，可重试",
            finished_at=time.time(),
        )
    from .recycle import record_cleanup

    record_cleanup(row)
    row["revision"] += 1
    store.put("recycle", row["id"], row)
    return row


def _exchange(left, right):
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(left), -100, os.fsencode(right), 2) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    fsync_dir(left.parent)


def _relative(root, path):
    path = Path(path)
    if not path.is_relative_to(root) or any(
        p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(root)
    ):
        raise ValueError(f"数据引用超出数据集或含符号链接：{path}")
    return path.relative_to(root)


def _snapshot(root):
    return {
        str(path.relative_to(root)): (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        for path in [root, *root.rglob("*")]
        if (stat := path.stat())
    }


def _write_table(path, table):
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)  # Metadata may be a hard link; never mutate it.
    pq.write_table(table, path)


def _jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def _scalar_stats(values):
    import numpy as np

    data = np.asarray(values, dtype=np.float64)
    return {
        "min": [float(data.min())],
        "max": [float(data.max())],
        "mean": [float(data.mean())],
        "std": [float(data.std())],
        "count": [len(data)],
    }


def _trim_video(source, output, segments, fps):
    """Remove unused time spans, preserving packets at independent GOP boundaries.

    Arbitrary external H.264 files may cross Episode boundaries. Decode/re-encode
    those affected shards instead of leaving references to deleted frames.
    """
    from fractions import Fraction

    import av

    ranges = [(float(start), float(end)) for start, end in segments]
    expected = sum(round((end - start) * fps) for start, end in ranges)
    with av.open(str(source)) as container:
        stream = container.streams.video[0]
        duration = (
            float(stream.duration * stream.time_base)
            if stream.duration is not None
            else None
        )
        if (
            len(ranges) == 1
            and abs(ranges[0][0]) < 1e-7
            and duration is not None
            and abs(ranges[0][1] - duration) < 1 / fps / 2
        ):
            os.link(source, output)
            return
        safe = not stream.codec_context.has_b_frames
        found = set()
        if safe:
            for packet in container.demux(stream):
                if packet.pts is None:
                    continue
                timestamp = float(packet.pts * stream.time_base)
                for i, (start, _end) in enumerate(ranges):
                    if abs(timestamp - start) < 1 / fps / 2:
                        safe &= packet.is_keyframe
                        found.add(i)
            safe &= len(found) == len(ranges)
    if safe:
        with av.open(str(source)) as src, av.open(str(output), "w") as dst:
            stream = src.streams.video[0]
            target = dst.add_stream_from_template(stream)
            target.time_base = stream.time_base
            offsets = []
            elapsed = 0.0
            for start, end in ranges:
                offsets.append(round((start - elapsed) / float(stream.time_base)))
                elapsed += end - start
            index, written = 0, 0
            for packet in src.demux(stream):
                if packet.pts is None or packet.dts is None:
                    continue
                timestamp = float(packet.pts * stream.time_base)
                while index < len(ranges) and timestamp >= ranges[index][1] - 1e-7:
                    index += 1
                if index == len(ranges):
                    break
                if timestamp < ranges[index][0] - 1e-7:
                    continue
                packet.pts -= offsets[index]
                packet.dts -= offsets[index]
                packet.stream = target
                dst.mux(packet)
                written += 1
            if written != expected:
                raise ValueError(f"视频帧数不符：{written} / {expected}")
    else:
        with av.open(str(source)) as src, av.open(str(output), "w") as dst:
            stream = src.streams.video[0]
            target = dst.add_stream("libx264", rate=fps)
            target.width, target.height = stream.width, stream.height
            target.pix_fmt = "yuv420p"
            target.options = {"crf": "18", "preset": "fast"}
            index, written = 0, 0
            for frame in src.decode(stream):
                timestamp = float(frame.pts * frame.time_base)
                while index < len(ranges) and timestamp >= ranges[index][1] - 1e-7:
                    index += 1
                if index == len(ranges):
                    break
                if timestamp < ranges[index][0] - 1e-7:
                    continue
                frame = frame.reformat(format="yuv420p")
                frame.pts, frame.time_base = written, Fraction(1, fps)
                for packet in target.encode(frame):
                    dst.mux(packet)
                written += 1
            for packet in target.encode():
                dst.mux(packet)
            if written != expected:
                raise ValueError(f"视频帧数不符：{written} / {expected}")
    # Decode every replacement to catch malformed cuts before publication.
    with av.open(str(output)) as check:
        actual = sum(1 for _ in check.decode(video=0))
    if actual != expected:
        raise ValueError(f"视频解码帧数不符：{actual} / {expected}")


def rebuild(root, stage, deleted, progress):
    import numpy as np
    import pyarrow as pa
    from letools._stats import aggregate_episode_stats, flatten_stats
    from letools.plugins import open_dataset
    from letools.validation import validate_dataset

    source = open_dataset(root)
    all_ids = [ep.index for ep in source.episodes]
    if (
        all_ids != list(range(len(all_ids)))
        or not deleted
        or not deleted <= set(all_ids)
    ):
        raise ValueError("Episode 索引或清理选择无效")
    kept = [ep for ep in source.episodes if ep.index not in deleted]
    mapping = {str(ep.index): i for i, ep in enumerate(kept)}
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise ValueError("数据集含符号链接，拒绝清理")
    # Copy ancillary files while preserving the original metadata until publish.
    shutil.copytree(
        root,
        stage,
        copy_function=os.link,
        ignore=lambda path, names: [
            n for n in names if Path(path) == root and n in {"data", "videos", MARKER}
        ],
    )
    info = copy.deepcopy(source.metadata.info)
    fps = int(info["fps"])
    version = info["codebase_version"]
    chunks_size = int(info.get("chunks_size", 1000))
    original_rows = {}
    if version == "v3.0":
        import pyarrow.parquet as pq

        for file in sorted((root / "meta/episodes").rglob("*.parquet")):
            original_rows.update(
                {
                    int(row["episode_index"]): row
                    for row in pq.read_table(file).to_pylist()
                }
            )
    else:
        original_rows = {
            int(row["episode_index"]): row
            for line in (root / "meta/episodes.jsonl").read_text().splitlines()
            if line.strip()
            for row in [json.loads(line)]
        }
    rows, stats, global_offset = [], [], 0
    groups = defaultdict(list)
    for ep in kept:
        _relative(root, ep.data_path)
        groups[ep.data_path].append(ep)
    total_groups = len(groups)
    for number, episodes in enumerate(groups.values()):
        progress(f"正在整理表格 {number + 1}/{total_groups}")
        tables = []
        if version == "v3.0":
            chunk, file = divmod(number, chunks_size)
            data_path = info["data_path"].format(chunk_index=chunk, file_index=file)
        for ep in episodes:
            new_id = mapping[str(ep.index)]
            table = source.read_episode(ep)
            for name, values in {
                "episode_index": np.full(ep.length, new_id, dtype=np.int64),
                "index": np.arange(
                    global_offset, global_offset + ep.length, dtype=np.int64
                ),
            }.items():
                if name in table.column_names:
                    field = table.schema.field(name)
                    table = table.set_column(
                        table.schema.get_field_index(name),
                        field,
                        pa.array(values, type=field.type),
                    )
            ep_stats = copy.deepcopy(ep.stats)
            for name in ("episode_index", "index"):
                if name in table.column_names:
                    ep_stats[name] = _scalar_stats(table[name].to_numpy())
            stats.append({"episode_index": new_id, "stats": ep_stats})
            row = {
                key: value
                for key, value in original_rows[ep.index].items()
                if not key.startswith("stats/")
            }
            row.update(episode_index=new_id, tasks=list(ep.tasks), length=ep.length)
            if version == "v3.0":
                row.update(
                    {
                        "data/chunk_index": chunk,
                        "data/file_index": file,
                        "dataset_from_index": global_offset,
                        "dataset_to_index": global_offset + ep.length,
                        "meta/episodes/chunk_index": 0,
                        "meta/episodes/file_index": 0,
                        **flatten_stats(ep_stats),
                    }
                )
                tables.append(table)
            else:
                data_path = info["data_path"].format(
                    episode_index=new_id, episode_chunk=new_id // chunks_size
                )
                _write_table(stage / data_path, table)
            rows.append(row)
            global_offset += ep.length
        if version == "v3.0":
            _write_table(stage / data_path, pa.concat_tables(tables))
    rows.sort(key=lambda r: r["episode_index"])
    by_id = {row["episode_index"]: row for row in rows}
    for camera in source.metadata.video_keys:
        video_groups = defaultdict(list)
        for ep in kept:
            video = ep.videos[camera]
            _relative(root, video.path)
            video_groups[video.path].append(ep)
        for number, (path, episodes) in enumerate(video_groups.items()):
            progress(f"正在清理视频 {camera} · {number + 1}/{len(video_groups)}")
            if version == "v2.1":
                for ep in episodes:
                    new_id = mapping[str(ep.index)]
                    output = stage / info["video_path"].format(
                        video_key=camera,
                        episode_index=new_id,
                        episode_chunk=new_id // chunks_size,
                    )
                    output.parent.mkdir(parents=True, exist_ok=True)
                    os.link(path, output)
                continue
            chunk, file = divmod(number, chunks_size)
            output = stage / info["video_path"].format(
                video_key=camera, chunk_index=chunk, file_index=file
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            segments = [
                (ep.videos[camera].start, ep.videos[camera].end) for ep in episodes
            ]
            # Coalesce adjacent retained spans so unchanged shared files stay links.
            merged = []
            for start, end in segments:
                if merged and abs(start - merged[-1][1]) < 1e-6:
                    merged[-1] = (merged[-1][0], end)
                else:
                    merged.append((start, end))
            _trim_video(path, output, merged, fps)
            elapsed = 0.0
            for ep in episodes:
                prefix = f"videos/{camera}"
                by_id[mapping[str(ep.index)]].update(
                    {
                        f"{prefix}/chunk_index": chunk,
                        f"{prefix}/file_index": file,
                        f"{prefix}/from_timestamp": elapsed,
                        f"{prefix}/to_timestamp": elapsed + ep.length / fps,
                    }
                )
                elapsed += ep.length / fps
    splits = {}
    for name, bounds in info.get("splits", {"train": f"0:{len(all_ids)}"}).items():
        start, end = (int(value) for value in bounds.split(":"))
        splits[name] = (
            f"{sum(ep.index < start for ep in kept)}:{sum(ep.index < end for ep in kept)}"
        )
    info.update(
        total_episodes=len(kept),
        total_frames=global_offset,
        splits=splits,
        bodypipeline_revision=str(time.time_ns()),
    )
    if version == "v2.1":
        info.update(
            total_chunks=(len(kept) + chunks_size - 1) // chunks_size,
            total_videos=len(kept) * len(source.metadata.video_keys),
        )
        _jsonl(stage / "meta/episodes.jsonl", rows)
        _jsonl(stage / "meta/episodes_stats.jsonl", stats)
    else:
        shutil.rmtree(stage / "meta/episodes")
        # Explicit schema allows a completely empty dataset to remain readable.
        schema = None
        if not rows:
            import pyarrow.parquet as pq

            schema = pq.read_schema(next((root / "meta/episodes").rglob("*.parquet")))
        _write_table(
            stage / "meta/episodes/chunk-000/file-000.parquet",
            pa.Table.from_pylist(rows, schema=schema),
        )
    atomic_json(stage / "meta/info.json", info)
    atomic_json(
        stage / "meta/stats.json",
        aggregate_episode_stats([row["stats"] for row in stats]) if stats else {},
    )
    progress("正在验证保留的数据和视频")
    report = validate_dataset(stage, deep=True)
    if not report.valid:
        raise ValueError("清理后数据校验失败：" + "; ".join(report.errors[:5]))
    # Reject index/data mismatches that a shared-shard rewrite could otherwise hide.
    checked = open_dataset(stage)
    offset = 0
    for ep in checked.episodes:
        table = checked.read_episode(ep)
        if "index" in table.column_names and table["index"].to_pylist() != list(
            range(offset, offset + ep.length)
        ):
            raise ValueError("清理后全局帧索引不连续")
        offset += ep.length
    return {
        "mapping": mapping,
        "total_episodes": len(kept),
        "deleted_frames": source.metadata.total_frames - global_offset,
    }


def run(store, key, operation):
    lock_path = store.path / "recycle-locks" / f"{key}.lock"
    lock_path.parent.mkdir(exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        row = store.get("recycle", key)
        op = row["operation"]
        if op["id"] != operation or op["state"] != "pending":
            return
        root, stage = Path(row["dataset_path"]), _stage(row)

        def progress(message):
            op.update(state="running", progress=message, pid=os.getpid())
            store.put("recycle", key, row)

        try:
            progress("正在检查数据目录")
            if not os.access(root.parent, os.W_OK | os.X_OK):
                raise PermissionError("没有数据集父目录的写入权限")
            for directory in [
                root,
                *(path for path in root.rglob("*") if path.is_dir()),
            ]:
                if not os.access(directory, os.W_OK | os.X_OK):
                    raise PermissionError(f"无法清理数据目录：{directory}")
            snapshot = _snapshot(root)
            result = rebuild(root, stage, set(op["episodes"]), progress)
            atomic_json(stage / MARKER, {**result, "operation": operation})
            # atomic_json uses private state-file permissions. Dataset metadata
            # must keep the original team's access after directory publication.
            for path in [stage, *stage.rglob("*")]:
                original = root / path.relative_to(stage)
                if original.exists():
                    os.chmod(path, stat.S_IMODE(original.stat().st_mode))
                elif path.is_dir():
                    os.chmod(path, stat.S_IMODE(root.stat().st_mode))
                else:
                    # New metadata (including the transaction marker) must not
                    # revert a shared dataset to atomic_json's private 0600.
                    os.chmod(path, stat.S_IMODE(root.stat().st_mode) & 0o666)
            # Flush newly created payloads before exposing the new directory.
            for path in stage.rglob("*"):
                if path.is_file():
                    with path.open("rb") as file:
                        os.fsync(file.fileno())
            for directory in sorted(
                (path for path in stage.rglob("*") if path.is_dir()), reverse=True
            ):
                fsync_dir(directory)
            fsync_dir(stage)
            if snapshot != _snapshot(root):
                raise ValueError("清理期间数据集被其他进程修改，已取消本次清理")
            progress("正在提交清理结果")
            _exchange(root, stage)
            recover_operation(store, row)
        except Exception as exc:
            op["error"] = str(exc)
            recover_operation(store, row)
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--operation", required=True)
    args = parser.parse_args()
    run(Store(Path(args.store)), args.key, args.operation)


if __name__ == "__main__":
    main()
