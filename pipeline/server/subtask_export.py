"""Export an Argus annotation as an independent LeRobot dataset with subtasks.

Argus labels each arm's steps in seconds; a LeRobot frame carries exactly one
``subtask_index``.  This module turns the two arm timelines into one
non-overlapping, gap-aware frame sequence, keeps the vocabulary stable across
the whole dataset, and drives the copy / conversion / patch / verify pipeline
that publishes the result as a separately registered dataset.

Planning is pure Python: the frame ranges are derived from the episode's own
frame count and FPS, so the page can show the coverage before any Parquet file
is read.  Reading and rewriting Parquet happens in the isolated LeTools worker
(``subtask_export_worker.py``), which owns pyarrow.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from contextlib import ExitStack
from pathlib import Path

from pipeline.shared.collections import dataset_name
from pipeline.shared.core import DomainError, atomic_json, digest, executable_path, now

from . import argus_annotation
from .datasets import TARGET_FORMATS, dataset_for_job
from .merges import source_snapshot
from .recycle import dataset_lock
from .storage import ROBOT_LAYOUT, data_directory, share_data_tree, storage_path

# A frame no annotated step covers keeps -1.  It is never filled with an
# invented "idle" step and never with the episode's overall task.
UNANNOTATED_INDEX = -1
UNANNOTATED_TEXT = "未标注 / Unannotated"
TARGET_FORMAT = "v3.0"
NAME_SUFFIX = "-subtasks"
MAX_SEGMENT_TEXT = 400
MAX_SEGMENTS_PER_EPISODE = 2000
DEDUP_STATES = {
    "FAILED",
    "INTERRUPTED",
    "BLOCKED",
    "DELETING",
    "DELETE_FAILED",
    "DELETED",
}
ACTIVE = {"QUEUED", "RUNNING"}


def annotation_work(settings, row) -> Path:
    """The durable annotation directory, recorded or derived.

    Shared with :class:`~pipeline.server.annotations.AnnotationRunner` so a
    resume that moved its work directory keeps pointing at the same place.
    """

    recorded = row.get("work_path")
    if recorded:
        return Path(recorded)
    return storage_path(settings, row, "annotations", row["id"])


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _contexts(work: Path) -> dict[str, dict]:
    """Annotation episode id -> prepared context, indexed both ways."""

    rows: dict[str, dict] = {}
    directory = work / "argus" / "episodes"
    if not directory.is_dir():
        return rows
    for path in sorted(directory.glob("*/context.json")):
        data = _read_json(path)
        if not data:
            continue
        rows.setdefault(path.parent.name, data)
        episode = data.get("episode_id")
        if isinstance(episode, str) and episode:
            rows.setdefault(episode, data)
    return rows


def _segment_text(event: dict) -> str:
    verb = str(event.get("verb_class") or "").strip()
    obj = str(event.get("object") or "").strip()
    if not obj or obj.lower() in verb.lower():
        return (verb or obj)[:MAX_SEGMENT_TEXT]
    # `verb_class` often already names the object ("... toward the handheld
    # driver"), so appending `object` again would read like a second object.
    # Both fields come from the model and may disagree on the exact word
    # (driver / screwdriver), which is why the check allows shared stems.
    lowered = verb.lower()
    verb_words = [word for word in lowered.split() if len(word) > 4]
    object_words = [word for word in obj.lower().split() if len(word) > 4]
    mentioned = any(
        target in lowered
        or any(
            target.endswith(word) or word.endswith(target) for word in verb_words
        )
        for target in object_words
    )
    if mentioned:
        return verb[:MAX_SEGMENT_TEXT]
    return f"{verb} {obj}".strip()[:MAX_SEGMENT_TEXT]


def _events(data: dict) -> list[dict]:
    labels = data.get("labels") if isinstance(data.get("labels"), dict) else {}
    timeline = data.get("event_labels") or labels.get("timeline") or []
    rows = []
    for order, event in enumerate(timeline if isinstance(timeline, list) else []):
        if not isinstance(event, dict):
            continue
        start = _number(event.get("t_s"))
        if start is None:
            continue
        rows.append(
            {
                "order": order,
                "start": start,
                "end": _number(event.get("end_s")),
                "text": _segment_text(event),
            }
        )
    rows.sort(key=lambda row: (row["start"], row["order"]))
    return rows


def _compose(covering: list[dict]) -> str:
    """One description for every step active on the same frame.

    Overlapping arms each describe real work, and a frame can only point at one
    vocabulary entry, so the covering steps are joined in a stable order and
    identical wordings collapse.  Nothing is invented for the uncovered part.
    """

    texts: list[str] = []
    for row in sorted(covering, key=lambda row: (row["start"], row["order"])):
        if row["text"] and row["text"] not in texts:
            texts.append(row["text"])
    return " | ".join(texts)[:MAX_SEGMENT_TEXT]


def _timeline(events: list[dict], duration_s: float) -> list[tuple[float, float, str]]:
    """Non-overlapping labelled spans, adjacent equal spans merged."""

    for row in events:
        row["start"] = min(max(row["start"], 0.0), duration_s)
        end = row["end"]
        if end is None or end <= row["start"]:
            following = next(
                (r["start"] for r in events if r["start"] > row["start"]), duration_s
            )
            end = following
        row["end"] = min(max(end, row["start"]), duration_s)
    points = sorted({0.0, duration_s, *(r["start"] for r in events), *(r["end"] for r in events)})
    merged: list[tuple[float, float, str]] = []
    for start, end in zip(points, points[1:]):
        if end <= start:
            continue
        middle = (start + end) / 2
        text = _compose([r for r in events if r["start"] <= middle < r["end"]])
        if not text:
            continue
        if merged and merged[-1][1] == start and merged[-1][2] == text:
            merged[-1] = (merged[-1][0], end, text)
        else:
            merged.append((start, end, text))
    return merged


def _frame_bounds(start_s: float, end_s: float, fps: float, frames: int) -> tuple[int, int]:
    """Half-open frame range of a labelled span.

    Frame ``i`` is labelled when ``i / fps`` lies inside ``[start_s, end_s)``.
    Converting to whole frames here (instead of comparing Parquet timestamps in
    the worker) keeps the preview and the written label identical, and avoids
    float32 rounding on the first and last frame of every step.
    """

    first = math.ceil(start_s * fps - 1e-6)
    stop = math.ceil(end_s * fps - 1e-6)
    return max(0, min(first, frames)), max(0, min(stop, frames))


def _frame_ranges(
    spans: list[tuple[float, float, str]], fps: float, frames: int, vocabulary: dict
) -> list[list[int]]:
    ranges: list[list[int]] = []
    for start_s, end_s, text in spans:
        first, stop = _frame_bounds(start_s, end_s, fps, frames)
        if stop <= first:
            continue
        if text not in vocabulary:
            vocabulary[text] = len(vocabulary)
        index = vocabulary[text]
        if ranges and ranges[-1][1] == first and ranges[-1][2] == index:
            ranges[-1][1] = stop
        else:
            ranges.append([first, stop, index])
    return ranges[:MAX_SEGMENTS_PER_EPISODE]


def _covered_frames(ranges: list[list[int]]) -> int:
    return sum(stop - first for first, stop, _ in ranges)


def _episode_index(episode_id: str, context: dict) -> tuple[int | None, str]:
    index = context.get("episode_index")
    if type(index) is int:
        return index, "context"
    # Prepared runs name episodes after their index (`episode_000012`); keep the
    # fallback explicit in the plan so an operator can see where it came from.
    suffix = episode_id.rsplit("_", 1)[-1]
    if suffix.isdigit():
        return int(suffix), "name"
    return None, ""


def _labels_available(data: dict) -> bool:
    return argus_annotation.labels_available(data)


def build_plan(settings, row) -> dict:
    """Everything the export needs, without reading a Parquet file.

    Returns the frame ranges per episode, the dataset-wide vocabulary, the
    annotation-to-dataset mapping and the coverage the page shows.
    """

    work = annotation_work(settings, row)
    payloads = argus_annotation.episode_results(work)
    contexts = _contexts(work)
    dataset = _read_json(Path(row["dataset_path"]) / "meta/info.json")
    total_episodes = dataset.get("total_episodes")
    if type(total_episodes) is not int or total_episodes < 1:
        raise DomainError("来源数据集缺少有效的 Episode 总数", 409)
    dataset_fps = _number(dataset.get("fps")) or 0.0
    vocabulary: dict[str, int] = {}
    episodes: list[dict] = []
    skipped: list[dict] = []
    seen: dict[int, str] = {}
    for episode_id in sorted(payloads):
        data = payloads[episode_id]
        if not _labels_available(data):
            skipped.append({"episode_id": episode_id, "reason": "模型未返回可用标注"})
            continue
        context = contexts.get(episode_id) or {}
        index, source = _episode_index(episode_id, context)
        if index is None:
            skipped.append(
                {"episode_id": episode_id, "reason": "无法确定该标注对应的数据集 Episode"}
            )
            continue
        if not 0 <= index < total_episodes:
            skipped.append(
                {
                    "episode_id": episode_id,
                    "reason": f"数据集没有 Episode {index}（共 {total_episodes} 个）",
                }
            )
            continue
        if index in seen:
            skipped.append(
                {
                    "episode_id": episode_id,
                    "reason": f"与 {seen[index]} 对应同一个数据集 Episode {index}",
                }
            )
            continue
        fps = _number(context.get("fps")) or dataset_fps
        frames = context.get("n_state_frames")
        if type(frames) is not int or frames < 1:
            duration = _number(data.get("duration_s"))
            frames = round(duration * fps) if duration and fps else 0
        if frames < 1 or fps <= 0:
            skipped.append(
                {"episode_id": episode_id, "reason": "标注缺少帧数或帧率，无法对齐逐帧标签"}
            )
            continue
        spans = _timeline(_events(data), frames / fps)
        ranges = _frame_ranges(spans, fps, frames, vocabulary)
        if not ranges:
            skipped.append(
                {"episode_id": episode_id, "reason": "事件时间线没有可落帧的区间"}
            )
            continue
        seen[index] = episode_id
        episodes.append(
            {
                "episode_id": episode_id,
                "episode_index": index,
                "index_source": source,
                "fps": fps,
                "frames": frames,
                "duration_s": round(frames / fps, 6),
                "labelled_frames": _covered_frames(ranges),
                "coverage": round(_covered_frames(ranges) / frames, 6),
                "segments": ranges,
            }
        )
    episodes.sort(key=lambda item: item["episode_index"])
    stated = [item["episode_index"] for item in episodes]
    if len(set(stated)) != len(stated):
        raise DomainError("标注 Episode 与数据集 Episode 的对应关系重复", 409)
    total_frames = sum(item["frames"] for item in episodes)
    labelled_frames = sum(item["labelled_frames"] for item in episodes)
    return {
        "dataset": {
            "path": row["dataset_path"],
            "name": row.get("dataset_name"),
            "format": dataset.get("codebase_version"),
            "robot_type": row.get("robot_type") or dataset.get("robot_type"),
            "total_episodes": total_episodes,
            "fps": dataset_fps,
        },
        "episodes": episodes,
        "skipped": skipped,
        "vocabulary": [
            {"subtask": text, "subtask_index": index}
            for text, index in sorted(vocabulary.items(), key=lambda pair: pair[1])
        ],
        "unannotated_index": UNANNOTATED_INDEX,
        "unannotated_text": UNANNOTATED_TEXT,
        "coverage": {
            "annotated_episodes": len(episodes),
            "dataset_episodes": total_episodes,
            "unannotated_episodes": total_episodes - len(episodes),
            "frames": total_frames,
            "labelled_frames": labelled_frames,
            "unannotated_frames": total_frames - labelled_frames,
            "ratio": round(labelled_frames / total_frames, 6) if total_frames else 0.0,
            "labelled_ratio": round(len(episodes) / total_episodes, 6) if total_episodes else 0.0,
        },
        "digest": digest(
            {
                "dataset": row["dataset_path"],
                "source": row.get("source"),
                "episodes": [
                    [item["episode_index"], item["segments"]] for item in episodes
                ],
                "vocabulary": sorted(vocabulary.items(), key=lambda pair: pair[1]),
            }
        ),
    }


def plan_summary(plan: dict) -> dict:
    """The small part of a plan a job record may keep."""

    return {
        "annotated_episodes": plan["coverage"]["annotated_episodes"],
        "dataset_episodes": plan["coverage"]["dataset_episodes"],
        "frames": plan["coverage"]["frames"],
        "labelled_frames": plan["coverage"]["labelled_frames"],
        "ratio": plan["coverage"]["ratio"],
        "labelled_ratio": plan["coverage"]["labelled_ratio"],
        "vocabulary_size": len(plan["vocabulary"]),
        "skipped_episodes": len(plan["skipped"]),
        "unannotated_text": plan["unannotated_text"],
        "unannotated_index": plan["unannotated_index"],
    }


class SubtaskExportRunner:
    """Publish subtask datasets through the existing serial job queue."""

    def __init__(self, settings, runner):
        self.settings, self.runner, self.store = settings, runner, runner.store

    @staticmethod
    def _key(path) -> str:
        return hashlib.sha256(str(path).encode()).hexdigest()

    # ------------------------------------------------------------- inspection
    def preview(self, aid: str) -> dict:
        row = self.store.get("annotation", aid)
        try:
            plan = build_plan(self.settings, row)
        except DomainError as exc:
            return {
                "available": False,
                "reason": exc.message,
                "can_export": False,
                "annotation_state": row.get("state"),
                "exports": self.listing(aid),
            }
        exports = self.listing(aid)
        busy = any(item["state"] in ACTIVE for item in exports)
        return {
            "available": True,
            "reason": "",
            "can_export": bool(plan["episodes"]) and row.get("state") not in ACTIVE,
            "annotation_state": row.get("state"),
            "annotations_running": row.get("state") in ACTIVE,
            "export_running": busy,
            "dataset": plan["dataset"],
            "summary": plan_summary(plan),
            "episodes": [
                {
                    "episode_id": item["episode_id"],
                    "episode_index": item["episode_index"],
                    "frames": item["frames"],
                    "duration_s": item["duration_s"],
                    "labels": len(item["segments"]),
                    "coverage": item["coverage"],
                }
                for item in plan["episodes"]
            ],
            "skipped": plan["skipped"],
            "vocabulary": plan["vocabulary"],
            "exports": exports,
        }

    def listing(self, aid: str) -> list[dict]:
        rows = [
            {
                key: row.get(key)
                for key in (
                    "id",
                    "state",
                    "stage",
                    "dataset_name",
                    "annotation_id",
                    "created_at",
                    "started_at",
                    "finished_at",
                    "error",
                    "dataset_path",
                    "episode_count",
                    "subtask_report",
                )
            }
            for row in self.store.all("job")
            if row.get("operation") == "subtask_export"
            and (row.get("annotation_id") == aid or row.get("source_annotation_id") == aid)
        ]
        rows.sort(key=lambda row: float(row.get("created_at") or 0), reverse=True)
        return rows

    # ---------------------------------------------------------------- enqueue
    def enqueue(self, aid: str, name: str | None = None):
        tool = executable_path(self.settings.letools_bin)
        if not tool or not Path(tool).with_name("python").is_file():
            raise DomainError("生成 Subtask 数据集需要可执行的 LeTools Python 环境", 503)
        with self.runner.lock:
            row = self.store.get("annotation", aid)
            if row["state"] in ACTIVE:
                raise DomainError("标注任务仍在运行，请等待完成后再生成 Subtask 数据集", 409)
            # Resolve the registered dataset first: the annotation's recorded
            # path must still be the one the job publishes, otherwise the plan
            # would describe a dataset the export job never reads.
            source_job = self.store.get("job", row["job_id"])
            source = dataset_for_job(self.settings, source_job)
            if str(source) != row.get("dataset_path"):
                raise DomainError("来源数据集路径已变化，请创建新的标注任务", 409)
            plan = build_plan(self.settings, row)
            if not plan["episodes"]:
                raise DomainError("没有可导出的 Episode 标注；请先完成自动标注", 409)
            if plan["dataset"]["format"] not in TARGET_FORMATS:
                raise DomainError("Subtask 导出仅支持 LeRobot v2.1 / v3.0 数据集", 409)
            source_name = (
                row.get("dataset_name")
                or source_job.get("dataset_name")
                or row["job_id"]
            )
            if isinstance(name, str) and name.strip():
                output_name = dataset_name(name)
            else:
                output_name = dataset_name(f"{str(source_name)[:1980]}{NAME_SUFFIX}")
            key = "subtask-export:" + digest(
                {
                    "annotation_id": aid,
                    "source": row.get("source") or {},
                    "dataset": str(source),
                    "name": output_name,
                    "plan": plan["digest"],
                }
            )
            for existing in self.store.all("job"):
                if existing.get("key") == key and existing["state"] not in DEDUP_STATES:
                    return existing
            with ExitStack() as locks:
                locks.enter_context(dataset_lock(self.store, self._key(source)))
                snapshot = source_snapshot(source)
                job = {
                    "id": str(uuid.uuid4()),
                    "key": key,
                    "operation": "subtask_export",
                    "source_kind": "lerobot_subtask_export",
                    "annotation_id": aid,
                    "source_annotation_id": aid,
                    "source_job_id": row["job_id"],
                    "source_dataset_name": source_name,
                    "source_path": str(source),
                    "source_format": plan["dataset"]["format"],
                    "target_format": TARGET_FORMAT,
                    "source_snapshot": snapshot,
                    "source_episode_count": plan["dataset"]["total_episodes"],
                    "plan_digest": plan["digest"],
                    "subtask_summary": plan_summary(plan),
                    "dataset_name": output_name,
                    "instruction": "",
                    "state": "QUEUED",
                    "stage": "等待生成 Subtask 数据集",
                    "created_at": now(),
                    "error": None,
                    "robot_type": row.get("robot_type")
                    or source_job.get("robot_type")
                    or plan["dataset"]["robot_type"],
                    "capture_mode": "mock" if source_job.get("capture_mode") == "mock" else "subtask_export",
                    "processing_mode": "real",
                    "run_doctor": False,
                    "source_count": plan["dataset"]["total_episodes"],
                    "episode_count": plan["dataset"]["total_episodes"],
                    "frame_count": None,
                }
                for field in ("runtime_id", "provider", "profile", "profile_hash"):
                    if source_job.get(field) is not None:
                        job[field] = source_job[field]
                if self.settings.data_root is not None:
                    job["data_layout"] = ROBOT_LAYOUT
                destination = storage_path(self.settings, job, "work", job["id"])
                try:
                    inside = destination.is_relative_to(source)
                except (OSError, ValueError):
                    inside = False
                if inside:
                    raise DomainError("Subtask 输出不能位于来源数据集内", 400)
                self.store.put("job", job["id"], job)
                return job

    # -------------------------------------------------------------------- run
    def enqueue_review(self, dataset_id, body):
        """Freeze a saved human revision; subsequent draft edits cannot change it."""
        from .subtask_review import SubtaskReview

        tool = executable_path(self.settings.letools_bin)
        if not tool or not Path(tool).with_name("python").is_file():
            raise DomainError("生成修订数据集需要 LeTools Python 环境", 503)
        review = SubtaskReview(self.settings, self.store)
        with self.runner.lock:
            lock_key, source, _ = review.resolve(dataset_id)
            with dataset_lock(self.store, lock_key):
                self.runner.merges._check_recycle(source)
                source_job, source, snapshot, plan = review.export_plan(dataset_id, body)
                source_name = source_job.get("dataset_name") or dataset_id
                output_name = dataset_name(body.name or f"{str(source_name)[:1970]}-revised")
                key = "subtask-review:" + digest({"source": str(source), "plan": plan["digest"], "name": output_name})
                for existing in self.store.all("job"):
                    if existing.get("key") == key and existing["state"] not in DEDUP_STATES:
                        return existing
                job = {
                    "id": str(uuid.uuid4()), "key": key, "operation": "subtask_export",
                    "source_kind": "lerobot_subtask_revision",
                    "source_job_id": dataset_id, "source_dataset_name": source_name,
                    "source_annotation_id": source_job.get("source_annotation_id") or source_job.get("annotation_id"),
                    "source_path": str(source), "source_format": plan["dataset"]["format"],
                    "target_format": TARGET_FORMAT, "source_snapshot": snapshot,
                    "source_episode_count": plan["dataset"]["total_episodes"],
                    "plan_digest": plan["digest"], "revision_plan": plan,
                    "subtask_summary": plan_summary(plan), "dataset_name": output_name,
                    "instruction": "", "state": "QUEUED", "stage": "等待生成 Subtask 修订数据集",
                    "created_at": now(), "error": None,
                    "robot_type": source_job.get("robot_type") or plan["dataset"]["robot_type"],
                    "capture_mode": "mock" if source_job.get("capture_mode") == "mock" else "subtask_export",
                    "processing_mode": "real", "run_doctor": False,
                    "source_count": plan["dataset"]["total_episodes"],
                    "episode_count": plan["dataset"]["total_episodes"], "frame_count": None,
                }
                for field in ("runtime_id", "provider", "profile", "profile_hash"):
                    if source_job.get(field) is not None:
                        job[field] = source_job[field]
                if self.settings.data_root is not None:
                    job["data_layout"] = ROBOT_LAYOUT
                if storage_path(self.settings, job, "work", job["id"]).is_relative_to(source):
                    raise DomainError("修订输出不能位于来源数据集内", 400)
                self.store.put("job", job["id"], job)
                return job

    def run(self, job):
        save = self.runner.save
        save(job, state="RUNNING", stage="核验标注与来源数据集", started_at=now())
        try:
            source = Path(job["source_path"])
            with dataset_lock(self.store, self._key(source)):
                if job.get("source_kind") == "lerobot_subtask_revision":
                    self.runner.merges._check_recycle(source)
                    plan = job["revision_plan"]
                    if digest({k: v for k, v in plan.items() if k != "digest"}) != plan["digest"]:
                        raise DomainError("修订导出计划校验失败", 409)
                else:
                    annotation = self.store.get("annotation", job["annotation_id"])
                    plan = build_plan(self.settings, annotation)
                if plan["digest"] != job["plan_digest"]:
                    raise DomainError("标注结果在提交后发生变化，请重新生成 Subtask 数据集", 409)
                if not plan["episodes"]:
                    raise DomainError("标注结果已没有可导出的 Episode", 409)
                saved = job["source_snapshot"]
                if source_snapshot(source) != {
                    "signature": saved.get("signature"),
                    "bytes": saved.get("bytes"),
                }:
                    raise DomainError("来源数据集在提交后发生变化，请重新生成", 409)
                work = data_directory(
                    self.settings, job, "work", job["id"], exist_ok=False
                )
                save(job, work_path=str(work))
                size = int(saved.get("bytes") or 0)
                extra = size if job["source_format"] == TARGET_FORMAT else size * 2
                if (
                    shutil.disk_usage(work).free
                    < extra + extra // 4 + self.settings.min_free_bytes
                ):
                    raise DomainError("输出存储空间不足，无法创建独立 Subtask 数据集", 507)
                pending = work / "pending-dataset"
                # The queue writes this step's stdout to <name>.json, so the spec must not
                # share that name.
                spec_path = work / "subtask-spec.json"
                atomic_json(
                    spec_path,
                    {
                        "source": str(source),
                        "source_format": job["source_format"],
                        "target_format": TARGET_FORMAT,
                        "destination": str(pending),
                        "expected": {
                            "episodes": plan["dataset"]["total_episodes"],
                        },
                        "vocabulary": plan["vocabulary"],
                        "unannotated_index": plan["unannotated_index"],
                        "unannotated_text": plan["unannotated_text"],
                        "episodes": [
                            {
                                "episode_index": item["episode_index"],
                                "frames": item["frames"],
                                "segments": item["segments"],
                                **({"atoms": item["atoms"]} if "atoms" in item else {}),
                                **({"task_document": item["task_document"]} if "task_document" in item else {}),
                            }
                            for item in plan["episodes"]
                        ],
                    },
                )
                share_data_tree(work)
                save(job, stage="等待 Subtask 导出计算资源")
                allocation = self.runner._open_allocation(work)
                tool = executable_path(self.settings.letools_bin)
                if not tool:
                    raise DomainError("未找到 LeTools 环境", 503)
                save(
                    job,
                    stage=(
                        "复制数据集并写入 subtask_index"
                        if job["source_format"] == TARGET_FORMAT
                        else f"转换 {job['source_format']} → {TARGET_FORMAT} 并写入 subtask_index"
                    ),
                )
                code, report, err = self.runner.command(
                    [
                        str(Path(tool).with_name("python")),
                        str(Path(__file__).with_name("subtask_export_worker.py")),
                        str(spec_path),
                    ],
                    work,
                    "subtask-export",
                    allocation=allocation,
                )
                if code:
                    raise DomainError(
                        "Subtask 数据集生成失败："
                        + err.read_text(errors="replace")[-2000:]
                    )
                result = json.loads(report.read_text())
                info = _read_json(pending / "meta/info.json")
                if (
                    info.get("codebase_version") != TARGET_FORMAT
                    or info.get("total_episodes") != plan["dataset"]["total_episodes"]
                    or "subtask_index" not in (info.get("features") or {})
                    or result.get("validated") is not True
                ):
                    raise DomainError("Subtask 数据集的版本、Episode 总数或特征声明与计划不一致")
                save(job, stage="深度结构验证")
                code, checked, err = self.runner.command(
                    [tool, "validate", str(pending), "--deep"],
                    work,
                    "validate",
                    allocation=allocation,
                )
                if code:
                    raise DomainError(
                        "Subtask 数据集结构验证失败："
                        + err.read_text(errors="replace")[-2000:]
                    )
                if source_snapshot(source)["signature"] != saved.get("signature"):
                    raise DomainError("导出期间来源数据集发生变化，结果未发布")
                if self.runner.shutdown_requested.is_set():
                    raise InterruptedError("Worker 收到停止请求")
                lineage = {
                    "operation": "subtask_export",
                    "job_id": job["id"],
                    "annotation_id": job.get("annotation_id") or job.get("source_annotation_id"),
                    "review": plan.get("review"),
                    "source_job_id": job["source_job_id"],
                    "source_path": str(source),
                    "source_format": job["source_format"],
                    "target_format": TARGET_FORMAT,
                    "source_snapshot": saved,
                    "plan_digest": job["plan_digest"],
                    "subtask_summary": job["subtask_summary"],
                }
                atomic_json(pending / "meta/bodypipeline-subtask-export.json", lineage)
                atomic_json(work / "lineage.json", lineage)
                share_data_tree(work)
                destination = work / "dataset"
                os.replace(pending, destination)
                save(
                    job,
                    state="CONVERTED",
                    stage="Subtask 数据集已生成，可预览、质检或在数据集管理中删除",
                    dataset_ready=True,
                    dataset_path=str(destination),
                    episode_count=info.get("total_episodes"),
                    frame_count=info.get("total_frames"),
                    subtask_report=result,
                    finished_at=now(),
                )
        except InterruptedError as exc:
            save(
                job,
                state="INTERRUPTED",
                stage="Subtask 导出已中断",
                error=str(exc),
                finished_at=now(),
            )
        except DomainError as exc:
            save(
                job,
                state="FAILED",
                stage="Subtask 导出失败",
                error=exc.message,
                finished_at=now(),
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            save(
                job,
                state="FAILED",
                stage="Subtask 导出失败",
                error=f"{type(exc).__name__}: {exc}"[:1200],
                finished_at=now(),
            )
        finally:
            self.runner._close_allocation()
