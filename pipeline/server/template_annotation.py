"""Validate template-bound AI replies and recover lossless task documents.

Argus remains responsible for model calls, receipts, budget and resume. Read the
raw per-request replies here: its board projection intentionally omits custom
fields, and long recordings have request-local clocks and overlapping windows.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from pydantic import Field, StrictBool, StrictFloat, StrictInt, ValidationError

from pipeline.shared.core import digest

from .argus_template_contract import reply_identity
from .task_annotations import (
    AnnotationModel,
    TaskDocument,
    TaskTemplate,
    Value,
    validate_document,
)

SPEC_NAME = "task-template.json"
Time = StrictFloat | StrictInt


class AIAttempt(AnnotationModel):
    subtask_id: str
    start_s: Time = Field(ge=0)
    end_s: Time = Field(gt=0)
    values: dict[str, Value]
    continues_before: StrictBool = False
    continues_after: StrictBool = False


class AINull(AnnotationModel):
    start_s: Time = Field(ge=0)
    end_s: Time = Field(gt=0)
    note: str = Field(max_length=20000)


class AIFrame(AnnotationModel):
    t_s: Time = Field(ge=0)
    values: dict[str, Value]


class AIReply(AnnotationModel):
    template_id: str
    template_signature: str
    values: dict[str, Value]
    attempts: list[AIAttempt] = Field(max_length=2000)
    null_segments: list[AINull] = Field(max_length=2000)
    frames: list[AIFrame] = Field(max_length=10000)


def read_spec(work: Path) -> dict | None:
    path = work / SPEC_NAME
    if not path.exists():
        return None
    spec = json.loads(path.read_text())
    template = TaskTemplate.model_validate(spec["template"]).model_dump(
        exclude_none=True
    )
    if spec.get("version") != 1 or digest(template) != spec.get("signature"):
        raise ValueError("AI 标注的模板快照校验失败")
    return {**spec, "template": template}


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def _raw(work: Path, name: str) -> dict:
    for folder in ("run/out", "run_final/out"):
        for prefix in ("", "failed_", "noreply_"):
            path = work / "argus" / folder / f"{prefix}{name}.json"
            if path.exists():
                return _read(path)
    raise ValueError(f"{name} 尚未返回模板标注")


def _validate_unit(spec: dict, context: dict, ctx: dict, raw: dict):
    template = spec["template"]
    steps = {s["id"]: s["name"] for s in template["subtasks"]}
    fields = {f["id"]: f for f in template["fields"]}
    fps, count = context["fps"], context["n_state_frames"]
    if (
        type(fps) not in (int, float)
        or not math.isfinite(fps)
        or fps <= 0
        or type(count) is not int
        or count < 1
    ):
        raise ValueError("Episode 缺少有效帧率或帧数")

    def values(data, scope):
        if any(k not in fields or fields[k]["scope"] != scope for k in data):
            raise ValueError("AI 返回了模板之外的字段或错误的字段作用范围")
        return dict(data)

    if raw.get("parse_ok") is False or raw.get("dry_run") or raw.get("_label_failed"):
        raise ValueError("模型没有返回可用的模板标注")
    reply = AIReply.model_validate((raw.get("labels") or {}).get("bodypipeline_task"))
    if (
        reply.template_id != template["id"]
        or reply.template_signature != spec["signature"]
    ):
        raise ValueError("AI 返回的模板 ID 或版本与任务快照不一致")
    piece = ctx.get("piece") or {}
    if ctx.get("fps") != fps:
        raise ValueError("标注分段与 Episode 帧率不一致")
    offset = round(float(piece.get("t0_s", 0)) * fps)
    size = ctx["n_state_frames"]
    if type(size) is not int or size < 1 or offset < 0 or offset + size > count:
        raise ValueError("标注分段超出 Episode 范围")
    # Validate EACH reply before clipping; overlap ownership must not hide a
    # malformed interval, unknown field, or out-of-range rating.
    doc = {
        "version": 1,
        "templateId": template["id"],
        "templateSnapshot": template,
        "values": values(reply.values, "episode"),
        "attempts": [],
        "nullSegments": [],
        "frames": [],
    }

    def bounds(a, b):
        if a >= b or b > size / fps + 1e-6:
            raise ValueError("AI 时间区间为空或超出请求范围")
        start, end = math.ceil(a * fps - 1e-6), math.ceil(b * fps - 1e-6)
        if start >= end:
            raise ValueError("AI 时间区间没有覆盖有效帧")
        return start, end

    for i, a in enumerate(reply.attempts):
        if a.subtask_id not in steps:
            raise ValueError("AI 返回了模板之外的 Subtask")
        start, end = bounds(a.start_s, a.end_s)
        if (a.continues_before and (not piece or start != 0 or offset == 0)) or (
            a.continues_after and (not piece or end != size or offset + size == count)
        ):
            raise ValueError("AI 的跨请求延续标记不在分段边界")
        doc["attempts"].append(
            {
                "id": f"a-{i}",
                "subtaskId": a.subtask_id,
                "subtaskName": steps[a.subtask_id],
                "start": start,
                "end": end,
                "values": values(a.values, "attempt"),
            }
        )
    for i, a in enumerate(reply.null_segments):
        start, end = bounds(a.start_s, a.end_s)
        doc["nullSegments"].append(
            {
                "id": f"n-{i}",
                "label": "NULL",
                "start": start,
                "end": end,
                "note": a.note,
            }
        )
    for i, a in enumerate(reply.frames):
        frame = round(a.t_s * fps)
        if not 0 <= frame < size or a.t_s >= size / fps:
            raise ValueError("AI 单帧标注超出请求范围")
        doc["frames"].append(
            {"id": f"f-{i}", "frame": frame, "values": values(a.values, "frame")}
        )
    validate_document(TaskDocument.model_validate(doc), size)
    return offset, offset + size, ctx, reply, doc


def make_document(
    spec: dict, context: dict, units: list[tuple[dict, dict]]
) -> tuple[dict, list[str]]:
    """Validate local replies, assign overlap ownership, then restore parent frames."""
    template = spec["template"]
    count = context["n_state_frames"]
    layout = [_validate_unit(spec, context, ctx, raw) for ctx, raw in units]
    layout.sort(key=lambda item: item[0])
    if (
        not layout
        or layout[0][0] != 0
        or layout[-1][1] != count
        or any(
            left[1] < right[0] or left[1] >= right[1]
            for left, right in zip(layout, layout[1:])
        )
    ):
        raise ValueError("AI 分段不完整，不能发布为完整 Episode 标注")
    result = {
        "version": 1,
        "templateId": template["id"],
        "templateSnapshot": template,
        "values": layout[0][4]["values"] if len(layout) == 1 else {},
        "attempts": [],
        "nullSegments": [],
        "frames": [],
    }
    warnings = (
        []
        if len(layout) == 1
        else ["长 Episode 按请求分段标注，整体字段留空，请人工评价。"]
    )
    continuing = {}
    for i, (offset, end, ctx, reply, doc) in enumerate(layout):
        lo = (layout[i - 1][1] + offset) // 2 if i else 0
        hi = (end + layout[i + 1][0]) // 2 if i + 1 < len(layout) else count
        for kind in ("attempts", "nullSegments", "frames"):
            for j, record in enumerate(doc[kind]):
                item = dict(record)
                item["id"] = (
                    "ai-"
                    + digest(
                        {
                            "template": spec["signature"],
                            "episode": context["episode_id"],
                            "part": i,
                            "kind": kind,
                            "row": j,
                        }
                    )[:32]
                )
                if kind == "frames":
                    item["frame"] += offset
                    if lo <= item["frame"] < hi:
                        result[kind].append(item)
                    continue
                item["start"], item["end"] = (
                    max(lo, item["start"] + offset),
                    min(hi, item["end"] + offset),
                )
                if item["start"] >= item["end"]:
                    continue
                if kind == "attempts":
                    flag = reply.attempts[j]
                    previous = result[kind][-1] if result[kind] else None
                    # Only explicit synthetic-cut continuations join. Adjacent
                    # real retries of the same Subtask remain separate attempts.
                    if (
                        i > 0
                        and previous
                        and previous["end"] == item["start"]
                        and previous["subtaskId"] == item["subtaskId"]
                        and continuing.get(previous["id"]) == i - 1
                        and flag.continues_before
                    ):
                        previous["end"] = item["end"]
                        previous["values"] = {
                            k: v
                            for k, v in previous["values"].items()
                            if k in item["values"] and item["values"][k] == v
                        }
                        continuing[previous["id"]] = i if flag.continues_after else -1
                        continue
                    continuing[item["id"]] = i if flag.continues_after else -1
                result[kind].append(item)
    for kind in ("attempts", "nullSegments"):
        result[kind].sort(key=lambda item: (item["start"], item["end"]))
    result["frames"].sort(key=lambda item: item["frame"])
    return validate_document(TaskDocument.model_validate(result), count), warnings


def assemble_results(work: Path, base: dict[str, dict]) -> dict[str, dict]:
    spec = read_spec(work)
    if spec is None:
        return base
    result = {}
    for path in sorted((work / "argus/episodes").glob("*/context.json")):
        name = path.parent.name
        value = {
            **base.get(name, {}),
            "id": name,
            "source": "template",
            "template_name": spec["template"]["name"],
        }
        try:
            context = _read(path)
            names = (context.get("pieces") or {}).get("parts") or [name]
            units = [
                (_read(work / "argus/pieces" / n / "context.json"), _raw(work, n))
                if n != name
                else (context, _raw(work, n))
                for n in names
            ]
            document, warnings = make_document(spec, context, units)
            value.update(task_document=document, template_warnings=warnings)
            value.pop("_label_failed", None)
            value["parse_ok"] = True
            value["event_labels"] = [
                {
                    "t_s": a["start"] / context["fps"],
                    "end_s": a["end"] / context["fps"],
                    "verb_class": a["subtaskName"],
                    "values": a["values"],
                }
                for a in document["attempts"]
            ]
        except (ValueError, KeyError, TypeError, OSError, ValidationError) as exc:
            # No free-text fallback: a failed template contract stays visibly
            # failed and cannot quietly grow the shared Subtask vocabulary.
            value["_label_failed"] = {
                "status": "template_invalid",
                "why": str(exc)[:1000],
            }
        result[name] = value
    return result


def invalid_received_units(work: Path) -> list[str]:
    """Explicit resume retries only malformed received replies, retaining receipts.

    Missing replies are handled by Argus itself. Valid completed units are never
    recharged just because another unit failed the template contract.
    """
    spec = read_spec(work)
    if spec is None:
        return []
    invalid = []
    for path in sorted((work / "argus/episodes").glob("*/context.json")):
        parent = _read(path)
        names = (parent.get("pieces") or {}).get("parts") or [path.parent.name]
        for name in names:
            try:
                raw = _raw(work, name)
            except (ValueError, OSError):
                continue
            # Do not force-resend ambiguous no-reply receipts: upstream controls
            # those, since an interrupted request may still have been billed.
            if (
                raw.get("response") is None
                and raw.get("response_text") is None
                and not raw.get("labels")
            ):
                continue
            try:
                ctx = (
                    _read(work / "argus/pieces" / name / "context.json")
                    if name != path.parent.name
                    else parent
                )
                _validate_unit(spec, parent, ctx, raw)
            except (ValueError, KeyError, TypeError, OSError):
                invalid.append(name)
    return sorted(set(invalid))


def retry_manifest(work: Path) -> dict:
    return {
        "version": 1,
        "invalid_replies": {
            reply_identity(
                _raw(work, name)
            ): "BodyPipeline 模板校验失败；显式续跑仅重试本条模型回复"
            for name in invalid_received_units(work)
        },
    }
