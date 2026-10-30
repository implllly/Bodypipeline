"""Server-owned Subtask drafts; source data and Argus results stay immutable."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from pipeline.shared.core import DomainError, digest, executable_path, now

from .datasets import dataset_for_job
from .merges import source_snapshot
from .recycle import RecycleBin, dataset_lock
from .subtask_export import UNANNOTATED_INDEX, UNANNOTATED_TEXT
from .task_annotations import (
    TaskDocument,
    TemplateLibrary,
    document_segments,
    initial_template,
    source_document,
    validate_document,
)
from .viewer_data import PreviewRegistry


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_frame: StrictInt = Field(ge=0)
    end_frame: StrictInt = Field(gt=0)
    text: str = Field(min_length=1, max_length=400)


class ManualAtom(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    role: Literal["user", "assistant", "system", "tool"]
    content: str | None = Field(default=None, max_length=20000)
    style: Literal["task_aug", "plan", "memory", "interjection", "vqa"] | None
    timestamp: float = Field(ge=0)
    camera: str | None = Field(default=None, max_length=256)
    tool_calls: list[dict[str, Any]] | None = Field(default=None, max_length=32)


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=0)
    source_signature: str = Field(min_length=64, max_length=64)
    segments: list[Segment] = Field(max_length=2000)
    # Optional for backwards compatibility with existing interval-only drafts.
    atoms: list[ManualAtom] | None = Field(default=None, max_length=10000)
    task_document: TaskDocument | None = None


class RevisionExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: StrictInt = Field(ge=0)
    source_signature: str = Field(min_length=64, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=2000)


def validate_segments(segments: list[dict], frames: int) -> list[dict]:
    ordered = sorted(segments, key=lambda s: (s["start_frame"], s["end_frame"]))
    end = 0
    for item in ordered:
        start, stop, text = item["start_frame"], item["end_frame"], item["text"].strip()
        if (
            not text
            or len(text) > 400
            or start < end
            or not 0 <= start < stop <= frames
        ):
            raise DomainError(
                "区间必须不重叠、文本非空，且满足 0 ≤ 开始帧 < 结束帧 ≤ 总帧数", 422
            )
        end = stop
    return [{**item, "text": item["text"].strip()} for item in ordered]


class SubtaskReview:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.registry = PreviewRegistry(store, settings.viewer_ttl)
        self.resolver = RecycleBin(settings, store, self.registry)

    def resolve(self, dataset_id):
        key, path = self.resolver.resolve(f"bp/{dataset_id}")
        job = self.store.get("job", dataset_id)
        if dataset_for_job(self.settings, job) != path:
            raise DomainError("预览与已登记数据集不匹配，请重新打开预览", 409)
        return key, path, job

    def _source(self, path, snapshot):
        key = digest({"path": str(path), "snapshot": snapshot, "reader": 3})
        try:
            return self.store.get("subtask_source", key)["data"]
        except DomainError as exc:
            if exc.code != 404:
                raise
        tool = executable_path(self.settings.letools_bin)
        python = Path(tool).with_name("python") if tool else None
        if python is None or not python.is_file():
            raise DomainError("查看 Subtask 需要 LeTools Python 环境", 503)
        try:
            result = subprocess.run(
                [
                    str(python),
                    str(Path(__file__).with_name("subtask_review_worker.py")),
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=self.settings.command_timeout,
                env=self.settings.tool_environment(),
            )
        except subprocess.TimeoutExpired as exc:
            raise DomainError("读取 Subtask 超时，请稍后重试", 504) from exc
        if result.returncode:
            raise DomainError("无法读取 Subtask：" + result.stderr[-1200:], 409)
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise DomainError("Subtask 读取器未返回有效结果", 502) from exc
        if source_snapshot(path) != snapshot:
            raise DomainError("读取期间来源发生变化，请重新加载", 409)
        self.store.put("subtask_source", key, {"data": data})
        return data

    def _draft(self, key, path, snapshot):
        try:
            row = self.store.get("subtask_draft", key)
        except DomainError as exc:
            if exc.code != 404:
                raise
            return {
                "id": key,
                "dataset_path": str(path),
                "source_signature": snapshot["signature"],
                "revision": 0,
                "episodes": {},
            }
        if row["source_signature"] != snapshot["signature"]:
            raise DomainError(
                "来源数据已变化，草稿不能自动套用；请先核对来源与历史草稿", 409
            )
        return row

    @staticmethod
    def _check(row, body):
        if (
            row["revision"] != body.revision
            or row["source_signature"] != body.source_signature
        ):
            raise DomainError(
                "草稿版本或来源已变化，请重新加载后再编辑（不会覆盖其他窗口）", 409
            )

    @staticmethod
    def _episode(data, episode):
        for item in data["episodes"]:
            if item["episode_index"] == episode:
                return item
        raise DomainError("Episode 不存在", 404)

    def _document(self, row, item, data, path, job):
        stored = row.get("task_documents", {}).get(
            str(item["episode_index"]), item.get("task_document")
        )
        if stored is not None:
            try:
                document = validate_document(
                    TaskDocument.model_validate(stored), item["frames"]
                )
            except ValidationError as exc:
                raise DomainError(
                    "已保存的任务标注文档无效，请核对来源和历史草稿", 409
                ) from exc
            segments = row["episodes"].get(str(item["episode_index"]), item["segments"])
            if document_segments(document) != segments:
                raise DomainError(
                    "任务标注文档与来源 Subtask 不一致，请核对来源和历史草稿", 409
                )
            return document
        templates = TemplateLibrary(self.store).read()["templates"]
        template = next(
            (t for t in templates if t["id"] == row.get("template_id")), None
        )
        if template is None:
            template = row.get("template_snapshot") or initial_template(
                data, str(path), job.get("dataset_name") or "任务模板"
            )
        segments = row["episodes"].get(str(item["episode_index"]), item["segments"])
        return validate_document(
            TaskDocument.model_validate(source_document(segments, template)),
            item["frames"],
        )

    def episode(self, dataset_id, episode):
        key, path, job = self.resolve(dataset_id)
        with dataset_lock(self.store, key):
            self.resolve(dataset_id)
            snapshot = source_snapshot(path)
            data = self._source(path, snapshot)
            item = self._episode(data, episode)
            row = self._draft(key, path, snapshot)
            return {
                "episode_index": episode,
                "fps": data["fps"],
                "frames": item["frames"],
                "revision": row["revision"],
                "source_signature": row["source_signature"],
                "has_draft": str(episode) in row["episodes"],
                "segments": row["episodes"].get(str(episode), item["segments"]),
                "atoms": row.get("atoms", {}).get(str(episode), item.get("atoms", [])),
                "task_document": self._document(row, item, data, path, job),
            }

    def save(self, dataset_id, episode, body):
        key, path, job = self.resolve(dataset_id)
        with dataset_lock(self.store, key):
            self.resolve(dataset_id)
            snapshot = source_snapshot(path)
            data = self._source(path, snapshot)
            item = self._episode(data, episode)
            row = self._draft(key, path, snapshot)
            self._check(row, body)
            segments = validate_segments(
                [s.model_dump() for s in body.segments], item["frames"]
            )
            if body.task_document is not None:
                document = validate_document(body.task_document, item["frames"])
                if document_segments(document) != segments:
                    raise DomainError(
                        "Subtask 区间与任务标注文档不一致，请重新加载后再保存", 422
                    )
            else:
                previous = self._document(row, item, data, path, job)
                try:
                    document = validate_document(
                        TaskDocument.model_validate(
                            source_document(
                                segments, previous["templateSnapshot"], previous
                            )
                        ),
                        item["frames"],
                    )
                except ValidationError as exc:
                    raise DomainError(
                        "Subtask 修改与已有任务标注冲突，请使用任务标注工作区调整", 422
                    ) from exc
            row.setdefault("task_documents", {})[str(episode)] = document
            row["template_id"] = document["templateId"]
            row["template_snapshot"] = document["templateSnapshot"]
            if body.atoms is not None:
                atoms = [a.model_dump() for a in body.atoms]
                if (
                    len(json.dumps(atoms, ensure_ascii=False).encode())
                    > 4 * 1024 * 1024
                ):
                    raise DomainError("单个 Episode 的手动标注不能超过 4 MiB", 422)
                for atom in atoms:
                    if (
                        not math.isfinite(atom["timestamp"])
                        or atom["timestamp"] >= item["frames"] / data["fps"]
                    ):
                        raise DomainError("手动标注时间戳必须位于 Episode 内", 422)
                    # Validate arbitrary tool arguments recursively (including NaN).
                    try:
                        json.dumps(atom, allow_nan=False)
                    except (ValueError, TypeError) as exc:
                        raise DomainError("手动标注含无效工具参数", 422) from exc
                row.setdefault("atoms", {})[str(episode)] = atoms
            row["episodes"][str(episode)] = segments  # [] is an authoritative deletion.
            row.update(revision=row["revision"] + 1, updated_at=now())
            # Keep immutable audit snapshots outside the dataset, before publishing
            # the head. A crash cannot publish a head without its history.
            history_id = f"{key}-{row['revision']}"
            self.store.put(
                "subtask_draft_history",
                history_id,
                {**row, "id": history_id, "draft_id": key},
            )
            self.store.put("subtask_draft", key, row)
            return {"revision": row["revision"], "segments": segments, "saved": True}

    def export_plan(self, dataset_id, body):
        """Called under the dataset lock by the serial export enqueue path."""
        key, path, job = self.resolve(dataset_id)
        snapshot = source_snapshot(path)
        row = self._draft(key, path, snapshot)
        self._check(row, body)
        data = self._source(path, snapshot)
        vocabulary, episodes = {}, []
        labelled = total = 0
        for item in data["episodes"]:
            segments = validate_segments(
                row["episodes"].get(str(item["episode_index"]), item["segments"]),
                item["frames"],
            )
            spans = []
            covered = 0
            for s in segments:
                index = vocabulary.setdefault(s["text"], len(vocabulary))
                spans.append([s["start_frame"], s["end_frame"], index])
                covered += s["end_frame"] - s["start_frame"]
            episodes.append(
                {
                    "episode_index": item["episode_index"],
                    "frames": item["frames"],
                    "segments": spans,
                    "atoms": row.get("atoms", {}).get(
                        str(item["episode_index"]), item.get("atoms", [])
                    ),
                    "task_document": self._document(row, item, data, path, job),
                }
            )
            labelled += covered
            total += item["frames"]
        info = json.loads((path / "meta/info.json").read_text())
        plan = {
            "dataset": {
                "format": info["codebase_version"],
                "total_episodes": len(episodes),
                "robot_type": info.get("robot_type"),
            },
            "episodes": episodes,
            "vocabulary": [
                {"subtask": text, "subtask_index": index}
                for text, index in vocabulary.items()
            ],
            "unannotated_index": UNANNOTATED_INDEX,
            "unannotated_text": UNANNOTATED_TEXT,
            "skipped": [],
            "coverage": {
                "annotated_episodes": sum(bool(e["segments"]) for e in episodes),
                "dataset_episodes": len(episodes),
                "frames": total,
                "labelled_frames": labelled,
                "ratio": labelled / total if total else 0,
                "labelled_ratio": sum(bool(e["segments"]) for e in episodes)
                / len(episodes)
                if episodes
                else 0,
            },
            "review": {
                "draft_id": key,
                "revision": row["revision"],
                "source_signature": row["source_signature"],
            },
        }
        plan["digest"] = digest(plan)
        return job, path, snapshot, plan
