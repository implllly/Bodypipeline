"""Robot-independent task templates and lossless annotation documents."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from pipeline.shared.core import DomainError, digest

Value = StrictStr | StrictInt | StrictFloat | StrictBool | list[StrictStr] | None
Identifier = StrictStr


class AnnotationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Subtask(AnnotationModel):
    id: Identifier = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=400)


class AnnotationField(AnnotationModel):
    id: Identifier = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=400)
    scope: Literal["episode", "attempt", "frame"]
    kind: Literal["rating", "text", "textarea", "number", "single", "multi", "bool"]
    options: list[str] | None = Field(default=None, max_length=200)


class TaskTemplate(AnnotationModel):
    id: Identifier = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=400)
    subtasks: list[Subtask] = Field(max_length=2000)
    fields: list[AnnotationField] = Field(max_length=200)

    @model_validator(mode="after")
    def valid(self):
        if not self.name.strip():
            raise ValueError("模板名称不能为空")
        for items, label in [(self.subtasks, "Subtask"), (self.fields, "字段")]:
            ids = [item.id for item in items]
            if len(set(ids)) != len(ids):
                raise ValueError(f"{label} ID 重复")
        names = [s.name.strip() for s in self.subtasks]
        if any(not name for name in names) or len(set(names)) != len(names):
            raise ValueError("Subtask 名称不能为空或重复")
        if any(s.id == "__bodypipeline_null__" for s in self.subtasks):
            raise ValueError("Subtask 使用了系统保留 ID")
        for field in self.fields:
            if not field.label.strip():
                raise ValueError("字段名称不能为空")
            if field.kind in {"single", "multi"}:
                options = field.options or []
                if (
                    not options
                    or any(not o.strip() or len(o) > 400 for o in options)
                    or len(set(options)) != len(options)
                ):
                    raise ValueError("选项不能为空或重复")
        return self


class Attempt(AnnotationModel):
    id: Identifier = Field(min_length=1, max_length=160)
    subtaskId: Identifier = Field(min_length=1, max_length=160)
    subtaskName: str = Field(min_length=1, max_length=400)
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(gt=0)
    values: dict[str, Value]


class NullSegment(AnnotationModel):
    id: Identifier = Field(min_length=1, max_length=160)
    label: Literal["NULL"]
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(gt=0)
    note: str = Field(max_length=20000)


class FrameNote(AnnotationModel):
    id: Identifier = Field(min_length=1, max_length=160)
    frame: StrictInt = Field(ge=0)
    values: dict[str, Value]


class TaskDocument(AnnotationModel):
    version: Literal[1]
    templateId: Identifier
    templateSnapshot: TaskTemplate
    values: dict[str, Value]
    attempts: list[Attempt] = Field(max_length=2000)
    nullSegments: list[NullSegment] = Field(default_factory=list, max_length=2000)
    frames: list[FrameNote] = Field(max_length=10000)

    @model_validator(mode="after")
    def valid(self):
        if self.templateId != self.templateSnapshot.id:
            raise ValueError("任务模板 ID 不匹配")
        records = [*self.attempts, *self.nullSegments, *self.frames]
        if len({r.id for r in records}) != len(records):
            raise ValueError("标注记录 ID 重复")
        end = 0
        for span in sorted(
            [*self.attempts, *self.nullSegments], key=lambda a: (a.start, a.end)
        ):
            if span.start < end or span.end <= span.start:
                raise ValueError("Subtask 和 NULL 区间不能重叠或为空")
            end = span.end
        fields = {f.id: f for f in self.templateSnapshot.fields}
        for scope, values in [
            ("episode", self.values),
            *(("attempt", a.values) for a in self.attempts),
            *(("frame", f.values) for f in self.frames),
        ]:
            if len(values) > 400:
                raise ValueError("单条记录的字段过多")
            for key, value in values.items():
                # Retain values whose template field was removed, losslessly.
                field = fields.get(key)
                if value is None or field is None:
                    continue
                valid = field.scope == scope
                if field.kind == "rating":
                    valid &= type(value) is int and 1 <= value <= 5
                elif field.kind == "number":
                    valid &= type(value) in (int, float)
                elif field.kind == "bool":
                    valid &= type(value) is bool
                elif field.kind in {"text", "textarea"}:
                    valid &= isinstance(value, str)
                elif field.kind == "single":
                    valid &= isinstance(value, str) and value in (field.options or [])
                elif field.kind == "multi":
                    valid &= isinstance(value, list) and all(
                        v in (field.options or []) for v in value
                    )
                if not valid:
                    raise ValueError(f"字段“{field.label}”的值或作用范围不正确")
        if (
            len(json.dumps(self.model_dump(), ensure_ascii=False).encode())
            > 4 * 1024 * 1024
        ):
            raise ValueError("单个 Episode 的任务标注不能超过 4 MiB")
        return self


def validate_document(document: TaskDocument, frames: int) -> dict:
    if any(a.end > frames for a in [*document.attempts, *document.nullSegments]) or any(
        f.frame >= frames for f in document.frames
    ):
        raise DomainError("任务标注超出 Episode 帧范围", 422)
    return document.model_dump(exclude_none=True)


def document_segments(document: dict) -> list[dict]:
    names = {
        s["id"]: s["name"].strip() for s in document["templateSnapshot"]["subtasks"]
    }
    return [
        {
            "start_frame": a["start"],
            "end_frame": a["end"],
            "text": names.get(a["subtaskId"], a["subtaskName"].strip()),
        }
        for a in sorted(document["attempts"], key=lambda a: (a["start"], a["end"]))
    ]


def source_document(
    segments: list[dict], template: dict, previous: dict | None = None
) -> dict:
    """Import explicit AI/source spans or reconcile an interval-only client."""
    template = deepcopy(template)
    steps = {s["name"].strip(): s["id"] for s in template["subtasks"]}
    previous = deepcopy(previous) if previous else {}
    previous_attempts = previous.get("attempts", [])
    attempts = []
    for s in segments:
        text = s["text"].strip()
        if text not in steps:
            identifier = "source-" + digest(text)[:24]
            # A shared template may have renamed this ID. Preserve the explicit
            # source label as an unmatched step rather than duplicating its ID.
            if any(s["id"] == identifier for s in template["subtasks"]):
                identifier = (
                    "source-"
                    + digest({"text": text, "steps": template["subtasks"]})[:24]
                )
            steps[text] = identifier
            template["subtasks"].append({"id": steps[text], "name": text})
        # Match unchanged records by boundaries before text; stable IDs and scores
        # survive a legacy-client rename without changing another repeated step.
        old = next(
            (
                a
                for a in previous_attempts
                if a["start"] == s["start_frame"] and a["end"] == s["end_frame"]
            ),
            None,
        )
        attempts.append(
            {
                "id": old["id"] if old else "source-" + digest(s)[:32],
                "subtaskId": steps[text],
                "subtaskName": text,
                "start": s["start_frame"],
                "end": s["end_frame"],
                "values": old["values"] if old else {},
            }
        )
    return {
        "version": 1,
        "templateId": template["id"],
        "templateSnapshot": template,
        "values": previous.get("values", {}),
        "attempts": attempts,
        "nullSegments": previous.get("nullSegments", []),
        "frames": previous.get("frames", []),
    }


def initial_template(data: dict, identity: str, name: str) -> dict:
    texts = list(
        dict.fromkeys(
            s["text"].strip() for e in data["episodes"] for s in e["segments"]
        )
    )
    return {
        "id": "dataset-" + digest(identity)[:24],
        "name": name[:400] or "任务模板",
        "subtasks": [{"id": "source-" + digest(t)[:24], "name": t} for t in texts],
        "fields": [
            {
                "id": "episode-quality",
                "label": "任务质量",
                "scope": "episode",
                "kind": "rating",
            },
            {
                "id": "episode-comment",
                "label": "整体评价",
                "scope": "episode",
                "kind": "textarea",
            },
            {
                "id": "attempt-outcome",
                "label": "执行结果",
                "scope": "attempt",
                "kind": "single",
                "options": ["成功", "失败", "无法判断"],
            },
            {
                "id": "attempt-quality",
                "label": "执行质量",
                "scope": "attempt",
                "kind": "rating",
            },
            {
                "id": "attempt-comment",
                "label": "片段备注",
                "scope": "attempt",
                "kind": "textarea",
            },
            {
                "id": "frame-comment",
                "label": "异常说明",
                "scope": "frame",
                "kind": "textarea",
            },
        ],
    }


class TemplateRequest(AnnotationModel):
    revision: StrictInt = Field(ge=0)
    templates: list[TaskTemplate] = Field(max_length=200)

    @model_validator(mode="after")
    def valid(self):
        if len({t.id for t in self.templates}) != len(self.templates):
            raise ValueError("模板 ID 重复")
        return self


class TemplateLibrary:
    def __init__(self, store):
        self.store = store

    def read(self):
        try:
            return self.store.get("task_templates", "library")
        except DomainError as exc:
            if exc.code != 404:
                raise
            return {"id": "library", "revision": 0, "templates": []}

    def save(self, body):
        with self.store.lock:
            current = self.read()
            if current["revision"] != body.revision:
                raise DomainError("任务模板已被其他窗口修改，请重新加载后再保存", 409)
            row = {
                "id": "library",
                "revision": body.revision + 1,
                "templates": [t.model_dump(exclude_none=True) for t in body.templates],
            }
            if len(json.dumps(row, ensure_ascii=False).encode()) > 4 * 1024 * 1024:
                raise DomainError("任务模板库不能超过 4 MiB", 422)
            self.store.put("task_templates", "library", row)
            return row
