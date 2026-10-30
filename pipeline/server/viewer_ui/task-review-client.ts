import type { LanguageAtom } from "../types/language.types";
import {
  readDocument,
  validateTemplate,
  type TaskDocument,
  type TaskTemplate,
  type Field,
  type Scope,
  type Value,
} from "./task-annotation-model";
import type { Review } from "./annotation-review-client";

export interface TemplateLibrary {
  revision: number;
  templates: TaskTemplate[];
}

/** Exactly one Subtask label per frame; NULL and gaps stay out of the vocabulary. */
export function taskSegments(doc: TaskDocument, count: number) {
  readDocument(JSON.stringify(doc), doc.templateSnapshot, count);
  const spans = [...doc.attempts, ...(doc.nullSegments || [])].sort(
    (a, b) => a.start - b.start || a.end - b.end,
  );
  let end = 0;
  for (const span of spans) {
    if (span.start < end)
      throw new Error("Subtask 和 NULL 区间不能重叠，请先调整边界");
    end = span.end;
  }
  const names = new Map(
    doc.templateSnapshot.subtasks.map((s) => [s.id, s.name.trim()]),
  );
  return [...doc.attempts]
    .sort((a, b) => a.start - b.start || a.end - b.end)
    .map((a) => ({
      start_frame: a.start,
      end_frame: a.end,
      text: names.get(a.subtaskId) || a.subtaskName.trim(),
    }));
}

export function taskReviewPayload(
  review: Review,
  atoms: LanguageAtom[],
  doc: TaskDocument,
) {
  return {
    revision: review.revision,
    source_signature: review.source_signature,
    segments: taskSegments(doc, review.frames),
    task_document: doc,
    atoms: atoms
      .filter((a) => a.style !== "subtask")
      .map(({ end_timestamp: _end, ...a }) => a),
  };
}

export function reviewSnapshot(
  atoms: LanguageAtom[],
  task_document: TaskDocument | null,
) {
  return JSON.stringify({ atoms, task_document });
}

/** Template changes preserve scores, frame notes, NULL and unmatched source steps. */
export function retargetDocument(
  doc: TaskDocument,
  template: TaskTemplate,
): TaskDocument {
  const next = {
    ...template,
    subtasks: [...template.subtasks],
    fields: [...template.fields],
  };
  for (const field of doc.templateSnapshot.fields) {
    if (
      field.id.includes(":history:") &&
      !next.fields.some((f) => f.id === field.id)
    )
      next.fields.push(field);
  }
  const archived = new Map<string, string>();
  const compatible = (value: Value, field: Field, scope: Scope) => {
    if (value == null) return true;
    if (field.scope !== scope) return false;
    switch (field.kind) {
      case "rating":
        return (
          typeof value === "number" &&
          Number.isInteger(value) &&
          value >= 1 &&
          value <= 5
        );
      case "number":
        return typeof value === "number" && Number.isFinite(value);
      case "bool":
        return typeof value === "boolean";
      case "single":
        return typeof value === "string" && !!field.options?.includes(value);
      case "multi":
        return (
          Array.isArray(value) && value.every((v) => field.options?.includes(v))
        );
      default:
        return typeof value === "string";
    }
  };
  const migrateValues = (values: Record<string, Value>, scope: Scope) => {
    const result = { ...values };
    for (const [id, value] of Object.entries(values)) {
      const field = template.fields.find((f) => f.id === id);
      const old = doc.templateSnapshot.fields.find((f) => f.id === id);
      if (!field || !old || compatible(value, field, scope)) continue;
      let archiveId = archived.get(id);
      if (!archiveId) {
        const prefix = `${id.slice(0, 100)}:history:${old.scope}:${old.kind}`;
        archiveId = prefix;
        for (let n = 1; next.fields.some((f) => f.id === archiveId); n++)
          archiveId = `${prefix}:${n}`;
        archived.set(id, archiveId);
        next.fields.push({
          ...old,
          id: archiveId,
          label: `${old.label.slice(0, 380)}（历史字段）`,
        });
      }
      result[archiveId] = value;
      delete result[id];
    }
    return result;
  };
  const attempts = doc.attempts.map((a) => {
    const oldName =
      doc.templateSnapshot.subtasks.find((s) => s.id === a.subtaskId)?.name ||
      a.subtaskName;
    let step =
      next.subtasks.find((s) => s.id === a.subtaskId) ||
      next.subtasks.find((s) => s.name.trim() === oldName.trim());
    if (!step) {
      step = { id: a.subtaskId, name: oldName };
      next.subtasks.push(step);
    }
    return {
      ...a,
      subtaskId: step.id,
      subtaskName: step.name,
      values: migrateValues(a.values, "attempt"),
    };
  });
  const values = migrateValues(doc.values, "episode");
  const frames = doc.frames.map((f) => ({
    ...f,
    values: migrateValues(f.values, "frame"),
  }));
  const invalid = validateTemplate(next);
  if (invalid) throw new Error(invalid);
  return {
    ...doc,
    templateId: next.id,
    templateSnapshot: next,
    attempts,
    values,
    frames,
  };
}

export function readTaskReview(review: Review): TaskDocument {
  if (!review.task_document)
    throw new Error(
      "服务端尚未支持统一任务标注，请更新 Server 和 Viewer 后重试",
    );
  const doc = readDocument(
    JSON.stringify(review.task_document),
    review.task_document.templateSnapshot,
    review.frames,
  );
  const invalid = validateTemplate(doc.templateSnapshot);
  if (invalid) throw new Error(invalid);
  if (
    JSON.stringify(taskSegments(doc, review.frames)) !==
    JSON.stringify(
      review.segments.map((s) => ({
        start_frame: s.start_frame,
        end_frame: s.end_frame,
        text: s.text,
      })),
    )
  )
    throw new Error("服务端的 Subtask 与任务标注文档不一致，请核对来源后重试");
  return doc;
}
