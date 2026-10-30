"""Read frame-aligned subtasks in the isolated LeTools environment (read only)."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def language_atom(raw, fallback=0):
    """Match the original viewer's language-column coercion and defaults."""
    return {
        "role": raw["role"],
        "style": raw.get("style"),
        "content": raw.get("content"),
        "timestamp": raw.get("timestamp")
        if raw.get("timestamp") is not None
        else fallback,
        "camera": raw.get("camera") or None,
        "tool_calls": raw.get("tool_calls"),
        **(
            {"end_timestamp": raw["end_timestamp"]}
            if raw.get("end_timestamp") is not None
            else {}
        ),
    }


def coalesced(segments):
    result = []
    for s in sorted(segments, key=lambda s: (s["start_frame"], s["end_frame"])):
        if (
            result
            and result[-1]["end_frame"] == s["start_frame"]
            and result[-1]["text"] == s["text"]
        ):
            result[-1]["end_frame"] = s["end_frame"]
        else:
            result.append(dict(s))
    return result


def read_subtasks(root: Path) -> dict:
    import pyarrow.parquet as pq

    info = json.loads((root / "meta/info.json").read_text())
    fps = info.get("fps")
    if (
        info.get("codebase_version") not in {"v2.1", "v3.0"}
        or type(fps) not in (int, float)
        or not math.isfinite(fps)
        or fps <= 0
    ):
        raise ValueError("仅支持具有有效 FPS 的 LeRobot v2.1 / v3.0 数据集")
    vocabulary = {}
    vocab_path = root / "meta/subtasks.parquet"
    if vocab_path.is_file():
        # Pandas index metadata is intentionally not needed: Arrow exposes both
        # physical columns even when `subtask` is the pandas index.
        for row in pq.read_table(vocab_path).to_pylist():
            index, text = row["subtask_index"], row["subtask"]
            if type(index) is not int or not isinstance(text, str) or not text.strip():
                raise ValueError("Subtask 词表格式无效")
            if index in vocabulary:
                raise ValueError("Subtask 词表编号重复")
            vocabulary[index] = text
    episodes = {}
    for shard in sorted((root / "data").rglob("*.parquet")):
        reader = pq.ParquetFile(shard)
        columns = ["episode_index", "frame_index"]
        labelled = "subtask_index" in reader.schema_arrow.names
        if labelled:
            columns.append("subtask_index")
        for column in ("timestamp", "language_persistent", "language_events"):
            if column in reader.schema_arrow.names:
                columns.append(column)
        for batch in reader.iter_batches(batch_size=65536, columns=columns):
            data = batch.to_pydict()
            labels = data.get("subtask_index", [-1] * batch.num_rows)
            persistent_rows = data.get("language_persistent")
            event_rows = data.get("language_events")
            timestamps = data.get("timestamp")
            for offset, (ep, frame, label) in enumerate(
                zip(data["episode_index"], data["frame_index"], labels)
            ):
                if any(type(v) is not int for v in (ep, frame, label)) or ep < 0:
                    raise ValueError("Episode、帧或标签编号无效")
                item = episodes.setdefault(
                    ep,
                    {
                        "episode_index": ep,
                        "frames": 0,
                        "segments": [],
                        "atoms": [],
                        "persistent_seen": False,
                        "language_subtasks": [],
                        "indexed": labelled,
                    },
                )
                if frame != item["frames"]:
                    raise ValueError("Episode 帧序不连续或重复，无法安全编辑")
                item["frames"] += 1
                persistent = persistent_rows[offset] if persistent_rows else None
                events = event_rows[offset] if event_rows else None
                if persistent and not item["persistent_seen"]:
                    item["atoms"].extend(
                        language_atom(a)
                        for a in persistent
                        if a.get("role") and a.get("style") != "subtask"
                    )
                    item["language_subtasks"].extend(
                        language_atom(a)
                        for a in persistent
                        if a.get("role") and a.get("style") == "subtask"
                    )
                    item["persistent_seen"] = True
                for atom in events or []:
                    if atom.get("role"):
                        item["atoms"].append(
                            language_atom(
                                atom, timestamps[offset] if timestamps else frame / fps
                            )
                        )
                if label == -1:
                    continue
                if label < 0 or label not in vocabulary:
                    raise ValueError(f"Subtask 编号 {label} 没有有效词表项")
                text = vocabulary[label]
                segments = item["segments"]
                if (
                    segments
                    and segments[-1]["end_frame"] == frame
                    and segments[-1]["text"] == text
                ):
                    segments[-1]["end_frame"] += 1
                else:
                    segments.append(
                        {"start_frame": frame, "end_frame": frame + 1, "text": text}
                    )
                    if len(segments) > 2000:
                        raise ValueError("单个 Episode 超过 2000 段，暂不支持编辑")
    if len(episodes) != info.get("total_episodes") or sum(
        e["frames"] for e in episodes.values()
    ) != info.get("total_frames"):
        raise ValueError("实际 Episode / 帧数与数据集声明不一致")
    sidecar = root / "meta/bodypipeline-manual-annotations.json"
    manual = json.loads(sidecar.read_text())["episodes"] if sidecar.is_file() else {}
    task_path = root / "meta/bodypipeline-task-annotations.json"
    tasks = json.loads(task_path.read_text())["episodes"] if task_path.is_file() else {}
    for item in episodes.values():
        item.pop("persistent_seen")
        subtasks = sorted(item.pop("language_subtasks"), key=lambda a: a["timestamp"])
        if not item.pop("indexed"):
            for i, atom in enumerate(subtasks):
                start = round(float(atom["timestamp"]) * fps)
                end = round(
                    float(
                        atom.get("end_timestamp")
                        or (
                            subtasks[i + 1]["timestamp"]
                            if i + 1 < len(subtasks)
                            else item["frames"] / fps
                        )
                    )
                    * fps
                )
                if 0 <= start < end <= item["frames"] and atom.get("content"):
                    item["segments"].append(
                        {
                            "start_frame": start,
                            "end_frame": end,
                            "text": atom["content"],
                        }
                    )
        if str(item["episode_index"]) in manual:
            item["atoms"] = manual[str(item["episode_index"])]
        if str(item["episode_index"]) in tasks:
            # The Server validates the complete schema and bounds before use.
            document = tasks[str(item["episode_index"])]
            names = {
                s["id"]: s["name"].strip()
                for s in document["templateSnapshot"]["subtasks"]
            }
            segments = [
                {
                    "start_frame": a["start"],
                    "end_frame": a["end"],
                    "text": names.get(a["subtaskId"], a["subtaskName"].strip()),
                }
                for a in document["attempts"]
            ]
            if coalesced(segments) != coalesced(item["segments"]):
                raise ValueError("任务标注 sidecar 与逐帧 Subtask 标签不一致")
            item["segments"] = sorted(
                segments, key=lambda s: (s["start_frame"], s["end_frame"])
            )
            item["task_document"] = document
        if len(item["atoms"]) > 10000:
            raise ValueError("单个 Episode 超过 10000 条手动标注")
    return {"fps": fps, "episodes": [episodes[i] for i in sorted(episodes)]}


if __name__ == "__main__":
    print(json.dumps(read_subtasks(Path(sys.argv[1])), ensure_ascii=False))
