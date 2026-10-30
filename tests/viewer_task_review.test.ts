import { expect, test } from "bun:test";
import {
  defaultTemplate,
  emptyDocument,
} from "../pipeline/server/viewer_ui/task-annotation-model";
import {
  readTaskReview,
  retargetDocument,
  reviewSnapshot,
  taskReviewPayload,
} from "../pipeline/server/viewer_ui/task-review-client";
import type { Review } from "../pipeline/server/viewer_ui/annotation-review-client";

function fixture() {
  const template = defaultTemplate();
  const doc = emptyDocument(template);
  doc.values = { "episode-quality": 5 };
  doc.attempts = [
    {
      id: "failed",
      subtaskId: "step-2",
      subtaskName: "抓取目标",
      start: 0,
      end: 2,
      values: { "attempt-quality": 1 },
    },
    {
      id: "retry",
      subtaskId: "step-2",
      subtaskName: "抓取目标",
      start: 4,
      end: 6,
      values: { "attempt-quality": 5 },
    },
  ];
  doc.nullSegments = [
    { id: "wait", label: "NULL", start: 2, end: 3, note: "等待" },
  ];
  doc.frames = [{ id: "note", frame: 5, values: { "frame-comment": "稳定" } }];
  const review: Review = {
    fps: 10,
    frames: 6,
    revision: 3,
    source_signature: "f".repeat(64),
    segments: [],
    task_document: doc,
  };
  return { doc, review };
}

test("the studio saves exact attempt bounds, all scopes and NULL in one revision", () => {
  const { doc, review } = fixture();
  const payload = taskReviewPayload(
    review,
    [
      {
        role: "user",
        style: "vqa",
        content: "where?",
        timestamp: 0.1,
        camera: "top",
        tool_calls: null,
      },
    ],
    doc,
  );
  expect(payload.segments).toEqual([
    { start_frame: 0, end_frame: 2, text: "抓取目标" },
    { start_frame: 4, end_frame: 6, text: "抓取目标" },
  ]);
  expect(payload.task_document).toEqual(doc);
  expect(payload.atoms).toHaveLength(1);
  expect(readTaskReview({ ...review, segments: payload.segments })).toEqual(
    doc,
  );
  expect(payload.revision).toBe(3);
});

test("deleting a repeated attempt does not lose the other's score or frame notes", () => {
  const { doc, review } = fixture();
  const deleted = { ...doc, attempts: doc.attempts.slice(1) };
  const payload = taskReviewPayload(review, [], deleted);
  expect(payload.segments).toEqual([
    { start_frame: 4, end_frame: 6, text: "抓取目标" },
  ]);
  expect(payload.task_document.attempts[0].values["attempt-quality"]).toBe(5);
  expect(payload.task_document.frames).toEqual(doc.frames);
  expect(reviewSnapshot([], deleted)).not.toBe(reviewSnapshot([], doc));
});

test("template reuse retains unknown source steps and all custom values", () => {
  const { doc } = fixture();
  const template = {
    ...defaultTemplate(),
    id: "new-task",
    subtasks: [{ id: "new-grasp", name: "抓取目标" }],
  };
  const changed = retargetDocument(doc, template);
  expect(changed.attempts[0].subtaskId).toBe("new-grasp");
  expect(changed.attempts[1].values).toEqual(doc.attempts[1].values);
  expect(changed.values).toEqual(doc.values);
  expect(changed.nullSegments).toEqual(doc.nullSegments);
  const other = retargetDocument(doc, { ...template, subtasks: [] });
  expect(other.templateSnapshot.subtasks[0].name).toBe("抓取目标");
  expect(other.attempts[0].id).toBe("failed");
});

test("conflicting intervals and inconsistent server documents fail before save", () => {
  const { doc, review } = fixture();
  expect(() =>
    taskReviewPayload(review, [], {
      ...doc,
      attempts: [{ ...doc.attempts[0], end: 5 }, doc.attempts[1]],
    }),
  ).toThrow("重叠");
  expect(() =>
    taskReviewPayload(review, [], {
      ...doc,
      frames: [{ ...doc.frames[0], frame: 6 }],
    }),
  ).toThrow();
  expect(() => readTaskReview(review)).toThrow("不一致");
  expect(() => readTaskReview({ ...review, task_document: undefined })).toThrow(
    "更新 Server",
  );
});

test("changing a filled field's scope preserves its old value as a historical field", () => {
  const { doc } = fixture();
  const template = {
    ...doc.templateSnapshot,
    fields: doc.templateSnapshot.fields.map((f) =>
      f.id === "episode-quality" ? { ...f, scope: "frame" as const } : f,
    ),
  };
  const next = retargetDocument(doc, template);
  const history = next.templateSnapshot.fields.find((f) =>
    f.id.startsWith("episode-quality:history:"),
  )!;
  expect(history.scope).toBe("episode");
  expect(next.values[history.id]).toBe(5);
  expect(next.values["episode-quality"]).toBeUndefined();
  expect(doc.values["episode-quality"]).toBe(5);
  const expanded = retargetDocument(doc, {
    ...doc.templateSnapshot,
    fields: [
      ...doc.templateSnapshot.fields,
      { id: "extra", label: "共用新字段", scope: "episode", kind: "text" },
    ],
  });
  expect(expanded.values).toEqual(doc.values);
  expect(expanded.attempts).toEqual(doc.attempts);
});
