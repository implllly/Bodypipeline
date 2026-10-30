import { test, expect } from "bun:test";
import {
  NULL_TRACK_ID,
  attemptNumber,
  boundaryTime,
  conflictsWithNull,
  defaultTemplate,
  deleteTimelineSegment,
  documentKey,
  emptyDocument,
  externalSeekNeeded,
  frameAt,
  gestureRange,
  readDocument,
  timelineBoundary,
  timelineGesture,
  timelineTime,
  uid,
  validateRange,
  validateTemplate,
} from "../pipeline/server/viewer_ui/task-annotation-model";

test("record IDs work on HTTP LAN hosts without crypto.randomUUID", () => {
  let seed = 0;
  const source = {
    getRandomValues(bytes: Uint8Array) {
      seed++;
      bytes.forEach((_, i) => {
        bytes[i] = seed + i;
      });
      return bytes;
    },
  };
  const first = uid(source),
    second = uid(source);
  expect(first).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
  expect(first).not.toBe(second);
  expect(uid({ ...source, randomUUID: () => "native-id" })).toBe("native-id");
});

test("normal dragging always scrubs; only Shift can create, move, or resize", () => {
  for (const mode of ["create", "move", "start", "end"] as const) {
    expect(timelineGesture(mode, false)).toBe("scrub");
    expect(timelineGesture(mode, true)).toBe(mode);
  }
});
test("preview time follows the pointer continuously while annotations snap to frames", () => {
  const times = [0, 0.03, 0.12, 0.2];
  expect(timelineTime(0.24, 175, 100, 200)).toBeCloseTo(0.09);
  expect(timelineBoundary(times, 0.24, 175, 100, 200)).toBe(2);
  expect(times[timelineBoundary(times, 0.24, 175, 100, 200)]).toBe(0.12);
  expect(timelineTime(0.24, -50, 100, 200)).toBe(0);
  expect(timelineTime(0.24, 900, 100, 200)).toBe(0.24);
  expect(timelineTime(0.24, 200, 100, 0)).toBe(0);
});
test("even sub-frame external seeks update the video instead of snapping back", () => {
  expect(externalSeekNeeded(3, 3.05)).toBe(true);
  expect(externalSeekNeeded(3, 3.01)).toBe(true);
  expect(externalSeekNeeded(3, 3.199)).toBe(true);
  expect(externalSeekNeeded(3, 3)).toBe(false);
  expect(externalSeekNeeded(3, 3.00001)).toBe(false);
  expect(externalSeekNeeded(3, NaN)).toBe(false);
});

test("deleting a repeated subtask targets only its stable ID and is undoable", () => {
  const d = emptyDocument(defaultTemplate());
  d.values = { overall: 5 };
  d.attempts = [
    {
      id: "first",
      subtaskId: "grasp",
      subtaskName: "抓取",
      start: 1,
      end: 3,
      values: { result: "失败", score: 2 },
    },
    {
      id: "retry",
      subtaskId: "grasp",
      subtaskName: "抓取",
      start: 5,
      end: 7,
      values: { result: "成功", score: 5 },
    },
  ];
  d.nullSegments = [
    { id: "null", label: "NULL", start: 3, end: 5, note: "等待重试" },
  ];
  d.frames = [{ id: "frame", frame: 2, values: { note: "打滑" } }];
  const result = deleteTimelineSegment(d, "grasp", "retry");
  expect(result.doc.attempts).toEqual([d.attempts[0]]);
  expect(result.removed?.attempt).toEqual(d.attempts[1]);
  expect(result.doc.nullSegments).toEqual(d.nullSegments);
  expect(result.doc.frames).toEqual(d.frames);
  expect(result.doc.values).toEqual(d.values);
  expect(result.doc.templateSnapshot).toEqual(d.templateSnapshot);
  expect(d.attempts.length).toBe(2);
  const restored = {
    ...result.doc,
    attempts: [...result.doc.attempts, result.removed!.attempt!],
  };
  expect(
    readDocument(JSON.stringify(restored), defaultTemplate(), 10).attempts,
  ).toEqual(d.attempts);
  expect(
    deleteTimelineSegment(d, "different-track", "retry").removed,
  ).toBeNull();
  expect(deleteTimelineSegment(d, "grasp", "unknown").doc).toBe(d);
});
test("deleting NULL leaves all task attempts and the template intact", () => {
  const d = emptyDocument(defaultTemplate());
  d.attempts = [
    {
      id: "task",
      subtaskId: "s",
      subtaskName: "任务",
      start: 0,
      end: 3,
      values: { score: 4 },
    },
  ];
  d.nullSegments = [
    { id: "null", label: "NULL", start: 3, end: 5, note: "空闲" },
  ];
  const result = deleteTimelineSegment(d, NULL_TRACK_ID, "null");
  expect(result.doc.nullSegments).toEqual([]);
  expect(result.doc.attempts).toEqual(d.attempts);
  expect(result.removed?.nullSegment?.note).toBe("空闲");
  expect(deleteTimelineSegment(d, "s", "null").removed).toBeNull();
  expect(deleteTimelineSegment(d, NULL_TRACK_ID, "task").removed).toBeNull();
});

test("irregular source timestamps drive frame selection and terminal boundary", () => {
  const ts = [0, 0.03, 0.12, 0.2];
  expect(frameAt(ts, 0.09)).toBe(2);
  expect(frameAt(ts, 0.04)).toBe(1);
  expect(frameAt(ts, 0.24, false, 0.24)).toBe(3);
  expect(frameAt(ts, 0.24, true, 0.24)).toBe(4);
  expect(boundaryTime(ts, 4, 0.24)).toBe(0.24);
  expect(validateRange(2, 4, 4)).toBeNull();
  for (const [s, e] of [
    [2, 2],
    [-1, 2],
    [0, 5],
    [0.5, 2],
    [NaN, 3],
  ])
    expect(validateRange(s, e, 4)).not.toBeNull();
});
test("NULL is explicit background; old gaps stay unannotated", () => {
  const t = defaultTemplate(),
    d = emptyDocument(t);
  delete d.nullSegments;
  expect(readDocument(JSON.stringify(d), t, 10).nullSegments).toEqual([]);
  d.nullSegments = [
    { id: "background", label: "NULL", start: 2, end: 5, note: "等待" },
  ];
  const restored = readDocument(JSON.stringify(d), t, 10);
  expect(restored.nullSegments?.[0].label).toBe("NULL");
  expect(restored.attempts).toEqual([]);
  expect(attemptNumber(restored.attempts, "background")).toBe(0);
  d.nullSegments[0].end = 11;
  expect(() => readDocument(JSON.stringify(d), t, 10)).toThrow();
  t.subtasks.push({ id: NULL_TRACK_ID, name: "reserved" });
  expect(validateTemplate(t)).not.toBeNull();
});
test("pointer position snaps to source timestamps and terminal boundary", () => {
  const ts = [0, 0.03, 0.12, 0.2];
  expect(timelineBoundary(ts, 0.24, 175, 100, 200)).toBe(2);
  expect(timelineBoundary(ts, 0.24, 300, 100, 200)).toBe(4);
  expect(timelineBoundary(ts, 0.24, 900, 100, 200)).toBe(4);
  expect(timelineBoundary(ts, 0.24, 0, 100, 200)).toBe(0);
  expect(timelineBoundary(ts, 0.2, 300, 100, 200)).toBe(4);
});
test("forward and backward creation both produce valid half-open ranges", () => {
  expect(gestureRange("create", 3, 8, 10)).toEqual({ start: 3, end: 8 });
  expect(gestureRange("create", 8, 3, 10)).toEqual({ start: 3, end: 8 });
  expect(gestureRange("create", 10, 10, 10)).toEqual({ start: 9, end: 10 });
  expect(gestureRange("create", 0, 0, 10)).toEqual({ start: 0, end: 1 });
});
test("moving clamps both ends without changing length; resizing cannot cross", () => {
  const original = { start: 2, end: 6 };
  expect(gestureRange("move", 3, 5, 10, original)).toEqual({
    start: 4,
    end: 8,
  });
  expect(gestureRange("move", 3, -10, 10, original)).toEqual({
    start: 0,
    end: 4,
  });
  expect(gestureRange("move", 3, 50, 10, original)).toEqual({
    start: 6,
    end: 10,
  });
  expect(gestureRange("start", 2, 9, 10, original)).toEqual({
    start: 5,
    end: 6,
  });
  expect(gestureRange("end", 6, 0, 10, original)).toEqual({ start: 2, end: 3 });
  expect(original).toEqual({ start: 2, end: 6 });
});
test("NULL masks cannot contradict task segments; adjacent boundaries are valid", () => {
  const d = emptyDocument(defaultTemplate());
  d.attempts.push({
    id: "task",
    subtaskId: "s",
    subtaskName: "task",
    start: 2,
    end: 5,
    values: {},
  });
  d.nullSegments = [
    { id: "null", label: "NULL", start: 5, end: 7, note: "等待" },
  ];
  expect(conflictsWithNull(d, { start: 0, end: 2 }, true)).toBe(false);
  expect(conflictsWithNull(d, { start: 1, end: 3 }, true)).toBe(true);
  expect(conflictsWithNull(d, { start: 5, end: 6 }, false)).toBe(true);
  expect(conflictsWithNull(d, { start: 5, end: 7 }, true, "null")).toBe(false);
  expect(conflictsWithNull(d, { start: 7, end: 9 }, true)).toBe(false);
  expect(conflictsWithNull(d, { start: 1, end: 4 }, false)).toBe(false);
});
test("attempt identity is independent from subtask; retries are chronological", () => {
  const t = defaultTemplate(),
    d = emptyDocument(t);
  d.attempts = [
    {
      id: "retry",
      subtaskId: "grasp",
      subtaskName: "抓取",
      start: 6,
      end: 9,
      values: { result: "成功" },
    },
    {
      id: "first",
      subtaskId: "grasp",
      subtaskName: "抓取",
      start: 1,
      end: 4,
      values: { result: "失败" },
    },
    {
      id: "other",
      subtaskId: "move",
      subtaskName: "移动",
      start: 4,
      end: 6,
      values: {},
    },
  ];
  expect(attemptNumber(d.attempts, "retry")).toBe(2);
  expect(attemptNumber(d.attempts, "first")).toBe(1);
  expect(readDocument(JSON.stringify(d), t, 10).attempts[0].values.result).toBe(
    "成功",
  );
  expect(d.values).toEqual({});
});
test("browser keys isolate dataset, episode, and template", () => {
  const keys = [
    documentKey("bp/one", 0, "a"),
    documentKey("bp/two", 0, "a"),
    documentKey("bp/one", 1, "a"),
    documentKey("bp/one", 0, "b"),
  ];
  expect(new Set(keys).size).toBe(4);
  expect(documentKey("a:b", 0, "c")).not.toBe(documentKey("a", 0, "b:c"));
});
test("missing scores remain unannotated and corrupt/out-of-range data is rejected", () => {
  const t = defaultTemplate(),
    d = emptyDocument(t);
  expect(validateTemplate(t)).toBeNull();
  expect(d.values["episode-quality"]).toBeUndefined();
  expect(() => readDocument("{}", t, 4)).toThrow();
  d.frames = [{ id: "x", frame: 4, values: {} }];
  expect(() => readDocument(JSON.stringify(d), t, 4)).toThrow();
  t.subtasks.push({ id: "different", name: t.subtasks[0].name });
  expect(validateTemplate(t)).not.toBeNull();
});
