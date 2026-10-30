/** Task annotation contract, independent of robot-specific capture schemas. */
export type Scope = "episode" | "attempt" | "frame";
export type FieldKind =
  | "rating"
  | "text"
  | "textarea"
  | "number"
  | "single"
  | "multi"
  | "bool";
export type Value = string | number | boolean | string[] | null;
export interface Field {
  id: string;
  label: string;
  scope: Scope;
  kind: FieldKind;
  options?: string[];
}
export interface Subtask {
  id: string;
  name: string;
}
export interface TaskTemplate {
  id: string;
  name: string;
  subtasks: Subtask[];
  fields: Field[];
}
export interface Attempt {
  id: string;
  subtaskId: string;
  subtaskName: string;
  start: number;
  end: number;
  values: Record<string, Value>;
}
/** Explicitly annotated background, distinct from unannotated gaps. */
export interface NullSegment {
  id: string;
  label: "NULL";
  start: number;
  end: number;
  note: string;
}
export const NULL_TRACK_ID = "__bodypipeline_null__";
export interface FrameNote {
  id: string;
  frame: number;
  values: Record<string, Value>;
}
export interface TaskDocument {
  version: 1;
  templateId: string;
  templateSnapshot: TaskTemplate;
  values: Record<string, Value>;
  attempts: Attempt[];
  nullSegments?: NullSegment[];
  frames: FrameNote[];
}
/** Delete by stable record ID, never by subtask name or current selection. */
export function deleteTimelineSegment(
  doc: TaskDocument,
  track: string,
  id: string,
): {
  doc: TaskDocument;
  removed: { attempt?: Attempt; nullSegment?: NullSegment } | null;
} {
  if (track === NULL_TRACK_ID) {
    const record = doc.nullSegments?.find((s) => s.id === id);
    return record
      ? {
          doc: {
            ...doc,
            nullSegments: (doc.nullSegments || []).filter((s) => s.id !== id),
          },
          removed: { nullSegment: record },
        }
      : { doc, removed: null };
  }
  const record = doc.attempts.find((s) => s.id === id && s.subtaskId === track);
  return record
    ? {
        doc: { ...doc, attempts: doc.attempts.filter((s) => s.id !== id) },
        removed: { attempt: record },
      }
    : { doc, removed: null };
}
export const TEMPLATE_KEY = "bodypipeline:task-templates:v1";
export const bindingKey = (dataset: string) =>
  `bodypipeline:task-binding:v1:${encodeURIComponent(dataset)}`;
export const documentKey = (
  dataset: string,
  episode: number,
  template: string,
) =>
  `bodypipeline:task-annotations:v1:${encodeURIComponent(dataset)}:${episode}:${encodeURIComponent(template)}`;
/** randomUUID is unavailable on plain HTTP LAN deployments; getRandomValues is not. */
export function uid(
  source: {
    randomUUID?: () => string;
    getRandomValues: (bytes: Uint8Array) => Uint8Array;
  } = globalThis.crypto,
): string {
  if (typeof source.randomUUID === "function") return source.randomUUID();
  const bytes = source.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
export function defaultTemplate(): TaskTemplate {
  return {
    id: "general-task",
    name: "通用操作任务",
    subtasks: ["接近目标", "抓取目标", "移动目标", "放置目标"].map(
      (name, i) => ({ id: `step-${i + 1}`, name }),
    ),
    fields: [
      {
        id: "episode-quality",
        label: "任务质量",
        scope: "episode",
        kind: "rating",
      },
      {
        id: "episode-comment",
        label: "整体评价",
        scope: "episode",
        kind: "textarea",
      },
      {
        id: "attempt-outcome",
        label: "执行结果",
        scope: "attempt",
        kind: "single",
        options: ["成功", "失败", "无法判断"],
      },
      {
        id: "attempt-quality",
        label: "执行质量",
        scope: "attempt",
        kind: "rating",
      },
      {
        id: "attempt-comment",
        label: "片段备注",
        scope: "attempt",
        kind: "textarea",
      },
      {
        id: "frame-comment",
        label: "异常说明",
        scope: "frame",
        kind: "textarea",
      },
    ],
  };
}
export function emptyDocument(template: TaskTemplate): TaskDocument {
  return {
    version: 1,
    templateId: template.id,
    templateSnapshot: template,
    values: {},
    attempts: [],
    nullSegments: [],
    frames: [],
  };
}
export function validateTemplate(template: TaskTemplate): string | null {
  if (!template.name.trim()) return "请填写任务模板名称";
  if (
    !template.id ||
    !Array.isArray(template.subtasks) ||
    !Array.isArray(template.fields)
  )
    return "模板格式不正确";
  const steps = new Set<string>(),
    ids = new Set<string>(),
    names = new Set<string>();
  for (const step of template.subtasks) {
    if (
      !step.id ||
      step.id === NULL_TRACK_ID ||
      !step.name.trim() ||
      steps.has(step.id) ||
      names.has(step.name.trim())
    )
      return "子任务名称不能为空或重复，且不能使用系统保留 ID";
    steps.add(step.id);
    names.add(step.name.trim());
  }
  for (const f of template.fields) {
    if (!f.id || !f.label.trim() || ids.has(f.id))
      return "字段名称不能为空，字段 ID 不能重复";
    if (
      !["episode", "attempt", "frame"].includes(f.scope) ||
      ![
        "rating",
        "text",
        "textarea",
        "number",
        "single",
        "multi",
        "bool",
      ].includes(f.kind)
    )
      return "存在不支持的字段类型或作用范围";
    if (
      ["single", "multi"].includes(f.kind) &&
      (!f.options?.length ||
        f.options.some((o) => !o.trim()) ||
        new Set(f.options).size !== f.options.length)
    )
      return `“${f.label}”需要不重复的选项`;
    ids.add(f.id);
  }
  return null;
}
/** All saved ranges are half-open [start,end); a terminal boundary is allowed. */
export function validateRange(
  start: number,
  end: number,
  count: number,
): string | null {
  return Number.isInteger(start) &&
    Number.isInteger(end) &&
    start >= 0 &&
    end > start &&
    end <= count
    ? null
    : `片段需满足 0 ≤ 起始帧 < 结束边界 ≤ ${count}`;
}
export function frameAt(
  timestamps: number[],
  time: number,
  boundary = false,
  duration = 0,
): number {
  if (!timestamps.length || !Number.isFinite(time)) return 0;
  if (
    boundary &&
    time >= duration &&
    duration > timestamps[timestamps.length - 1]
  )
    return timestamps.length;
  let low = 0,
    high = timestamps.length - 1;
  while (low < high) {
    const mid = Math.floor((low + high) / 2);
    if (timestamps[mid] < time) low = mid + 1;
    else high = mid;
  }
  return low > 0 &&
    Math.abs(timestamps[low - 1] - time) <= Math.abs(timestamps[low] - time)
    ? low - 1
    : low;
}
export function boundaryTime(
  timestamps: number[],
  frame: number,
  duration: number,
): number {
  return frame >= timestamps.length ? duration : (timestamps[frame] ?? 0);
}
/** Convert pointer position using real source times, including the final edge. */
export function timelineBoundary(
  timestamps: number[],
  duration: number,
  x: number,
  left: number,
  width: number,
): number {
  if (!timestamps.length || width <= 0 || !Number.isFinite(x)) return 0;
  const ratio = Math.max(0, Math.min(1, (x - left) / width));
  if (ratio === 1) return timestamps.length;
  return frameAt(
    timestamps,
    timelineTime(duration, x, left, width),
    true,
    duration,
  );
}
/** Scrubbing keeps the pointer's exact time; only annotation edges snap. */
export function timelineTime(
  duration: number,
  x: number,
  left: number,
  width: number,
): number {
  if (duration <= 0 || width <= 0 || !Number.isFinite(x)) return 0;
  return Math.max(0, Math.min(1, (x - left) / width)) * duration;
}
export type RangeGesture = "create" | "move" | "start" | "end";
export type TimelineGesture = RangeGesture | "scrub";
export function timelineGesture(
  requested: RangeGesture,
  shift: boolean,
): TimelineGesture {
  return shift ? requested : "scrub";
}
/** External user seeks must not use the much looser playback drift tolerance. */
export function externalSeekNeeded(current: number, target: number): boolean {
  return Number.isFinite(target) && Math.abs(current - target) > 0.0001;
}
export function gestureRange(
  mode: RangeGesture,
  anchor: number,
  current: number,
  count: number,
  original?: { start: number; end: number },
): { start: number; end: number } {
  if (count < 1) return { start: 0, end: 0 };
  const clamp = (v: number, min: number, max: number) =>
    Math.max(min, Math.min(max, Math.round(v)));
  if (mode === "create" || !original) {
    const start = clamp(Math.min(anchor, current), 0, count - 1);
    return { start, end: clamp(Math.max(anchor, current), start + 1, count) };
  }
  if (mode === "start")
    return { start: clamp(current, 0, original.end - 1), end: original.end };
  if (mode === "end")
    return {
      start: original.start,
      end: clamp(current, original.start + 1, count),
    };
  const length = original.end - original.start;
  const start = clamp(original.start + current - anchor, 0, count - length);
  return { start, end: start + length };
}
export function conflictsWithNull(
  doc: TaskDocument,
  range: { start: number; end: number },
  background: boolean,
  exceptId?: string | null,
): boolean {
  const candidates = background
    ? [...doc.attempts, ...(doc.nullSegments || [])]
    : doc.nullSegments || [];
  return candidates.some(
    (s) => s.id !== exceptId && range.start < s.end && range.end > s.start,
  );
}
export function attemptNumber(attempts: Attempt[], id: string): number {
  const target = attempts.find((a) => a.id === id);
  if (!target) return 0;
  return (
    attempts
      .filter((a) => a.subtaskId === target.subtaskId)
      .sort(
        (a, b) =>
          a.start - b.start || a.end - b.end || a.id.localeCompare(b.id),
      )
      .findIndex((a) => a.id === id) + 1
  );
}
export function readDocument(
  raw: string,
  template: TaskTemplate,
  count: number,
): TaskDocument {
  const d = JSON.parse(raw) as TaskDocument;
  if (
    d.version !== 1 ||
    d.templateId !== template.id ||
    !d.values ||
    typeof d.values !== "object" ||
    Array.isArray(d.values) ||
    !Array.isArray(d.attempts) ||
    !Array.isArray(d.frames)
  )
    throw new Error("浏览器记录格式不正确，原记录已保留");
  const ids = new Set<string>();
  for (const a of d.attempts) {
    if (
      !a.id ||
      ids.has(a.id) ||
      !a.subtaskId ||
      !a.values ||
      validateRange(a.start, a.end, count)
    )
      throw new Error("浏览器中的片段与当前数据帧范围不匹配，原记录已保留");
    ids.add(a.id);
  }
  for (const f of d.frames) {
    if (
      !f.id ||
      ids.has(f.id) ||
      !f.values ||
      !Number.isInteger(f.frame) ||
      f.frame < 0 ||
      f.frame >= count
    )
      throw new Error("浏览器中的单帧记录无效，原记录已保留");
    ids.add(f.id);
  }
  if (d.nullSegments !== undefined && !Array.isArray(d.nullSegments))
    throw new Error("浏览器中的 NULL 片段格式不正确，原记录已保留");
  for (const segment of d.nullSegments || []) {
    if (
      !segment.id ||
      ids.has(segment.id) ||
      segment.label !== "NULL" ||
      typeof segment.note !== "string" ||
      validateRange(segment.start, segment.end, count)
    )
      throw new Error("浏览器中的 NULL 片段无效，原记录已保留");
    ids.add(segment.id);
  }
  return { ...d, nullSegments: d.nullSegments || [] };
}
