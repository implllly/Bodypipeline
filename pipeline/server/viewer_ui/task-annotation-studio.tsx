"use client";

import React, { useEffect, useRef, useState } from "react";
import { useAnnotations } from "../context/annotations-context";
import { useTime } from "../context/time-context";
import { TaskAnnotationTimeline } from "./task-annotation-timeline";
import { reviewRequest } from "../utils/annotation-review-client";
import {
  retargetDocument,
  taskSegments,
  type TemplateLibrary,
} from "../utils/task-review-client";
import {
  SegmentContextMenu,
  segmentMenuAnchor,
  type SegmentMenuAnchor,
} from "./task-annotation-menu";
import {
  NULL_TRACK_ID,
  TEMPLATE_KEY,
  attemptNumber,
  bindingKey,
  boundaryTime,
  conflictsWithNull,
  defaultTemplate,
  deleteTimelineSegment,
  documentKey,
  emptyDocument,
  frameAt,
  readDocument,
  uid,
  validateRange,
  validateTemplate,
  type Attempt,
  type Field,
  type FrameNote,
  type Scope,
  type NullSegment,
  type TaskDocument,
  type TaskTemplate,
  type Value,
} from "../utils/task-annotation-model";

const scopes: Record<Scope, string> = {
  episode: "整条 Episode",
  attempt: "时间片段",
  frame: "单帧",
};
const kinds: Record<Field["kind"], string> = {
  rating: "五星评分",
  text: "短文本",
  textarea: "长文本",
  number: "数字",
  single: "单选",
  multi: "多选",
  bool: "是 / 否",
};
const message = (e: unknown) => (e instanceof Error ? e.message : String(e));
const clock = (time: number) =>
  `${Math.floor(time / 60)
    .toString()
    .padStart(2, "0")}:${(time % 60).toFixed(2).padStart(5, "0")}`;

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: Field;
  value: Value | undefined;
  onChange: (value: Value) => void;
}) {
  const id = `ta-field-${field.id}`;
  return (
    <div className="ta-field">
      <label htmlFor={id}>{field.label}</label>
      {field.kind === "rating" ? (
        <div className="ta-rating" role="group" aria-label={field.label}>
          {[1, 2, 3, 4, 5].map((n) => (
            <button
              type="button"
              key={n}
              className={
                typeof value === "number" && n <= value ? "filled" : ""
              }
              aria-label={`${field.label} ${n} 星`}
              aria-pressed={value === n}
              onClick={() => onChange(value === n ? null : n)}
            >
              ★
            </button>
          ))}
          <span>{typeof value === "number" ? `${value} / 5` : "未评分"}</span>
          {value != null && (
            <button
              type="button"
              className="ta-clear"
              onClick={() => onChange(null)}
              aria-label={`清空${field.label}`}
            >
              清空
            </button>
          )}
        </div>
      ) : field.kind === "textarea" ? (
        <textarea
          id={id}
          rows={3}
          value={String(value ?? "")}
          placeholder={`填写${field.label}…`}
          onChange={(e) => onChange(e.target.value || null)}
        />
      ) : field.kind === "single" || field.kind === "bool" ? (
        <select
          id={id}
          value={value == null ? "" : String(value)}
          onChange={(e) =>
            onChange(
              e.target.value === ""
                ? null
                : field.kind === "bool"
                  ? e.target.value === "true"
                  : e.target.value,
            )
          }
        >
          <option value="">未标注</option>
          {(field.kind === "bool"
            ? [
                { v: "true", l: "是" },
                { v: "false", l: "否" },
              ]
            : (field.options || []).map((v) => ({ v, l: v }))
          ).map((o) => (
            <option key={o.v} value={o.v}>
              {o.l}
            </option>
          ))}
        </select>
      ) : field.kind === "multi" ? (
        <div className="ta-choices" role="group" aria-label={field.label}>
          {field.options?.map((option) => (
            <label key={option}>
              <input
                type="checkbox"
                checked={Array.isArray(value) && value.includes(option)}
                onChange={(e) =>
                  onChange(
                    e.target.checked
                      ? [...(Array.isArray(value) ? value : []), option]
                      : (Array.isArray(value) ? value : []).filter(
                          (v) => v !== option,
                        ),
                  )
                }
              />
              {option}
            </label>
          ))}
        </div>
      ) : (
        <input
          id={id}
          type={field.kind === "number" ? "number" : "text"}
          step="any"
          value={value == null ? "" : String(value)}
          onChange={(e) =>
            onChange(
              e.target.value === ""
                ? null
                : field.kind === "number"
                  ? Number(e.target.value)
                  : e.target.value,
            )
          }
        />
      )}
    </div>
  );
}
function Fields({
  template,
  scope,
  values,
  onChange,
}: {
  template: TaskTemplate;
  scope: Scope;
  values: Record<string, Value>;
  onChange: (values: Record<string, Value>) => void;
}) {
  const fields = template.fields.filter((f) => f.scope === scope);
  return (
    <div className="ta-fields">
      {fields.length ? (
        fields.map((field) => (
          <FieldInput
            key={field.id}
            field={field}
            value={values[field.id]}
            onChange={(value) => onChange({ ...values, [field.id]: value })}
          />
        ))
      ) : (
        <p className="ta-muted">此范围还没有字段，可在“管理任务模板”中添加。</p>
      )}
    </div>
  );
}

function TemplateEditor({
  initial,
  onSave,
  onClose,
  serverError = "",
  busy = false,
}: {
  initial: TaskTemplate;
  onSave: (template: TaskTemplate) => void;
  onClose: () => void;
  serverError?: string;
  busy?: boolean;
}) {
  const [draft, setDraft] = useState<TaskTemplate>(() =>
    structuredClone(initial),
  );
  const [error, setError] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  const setField = (id: string, patch: Partial<Field>) =>
    setDraft((d) => ({
      ...d,
      fields: d.fields.map((f) => (f.id === id ? { ...f, ...patch } : f)),
    }));
  return (
    <dialog ref={dialog} className="ta-dialog" onCancel={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          const clean = {
            ...draft,
            name: draft.name.trim(),
            subtasks: draft.subtasks.map((s) => ({
              ...s,
              name: s.name.trim(),
            })),
            fields: draft.fields.map((f) => ({
              ...f,
              label: f.label.trim(),
              options: f.options?.map((o) => o.trim()).filter(Boolean),
            })),
          };
          const invalid = validateTemplate(clean);
          if (invalid) setError(invalid);
          else onSave(clean);
        }}
      >
        <header className="ta-dialog-head">
          <div>
            <span className="ta-kicker">TASK TEMPLATE</span>
            <h2>管理任务模板</h2>
          </div>
          <button type="button" aria-label="关闭模板编辑器" onClick={onClose}>
            关闭
          </button>
        </header>
        <p className="ta-muted">
          同类任务共用子任务清单和字段；每条数据、每次尝试的填写结果独立保存。
        </p>
        <label className="ta-field">
          任务模板名称
          <input
            aria-label="任务模板名称"
            value={draft.name}
            onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
          />
        </label>
        <div className="ta-section-head">
          <h3>固定的 Subtask 清单</h3>
          <button
            type="button"
            onClick={() =>
              setDraft((d) => ({
                ...d,
                subtasks: [...d.subtasks, { id: uid(), name: "" }],
              }))
            }
          >
            ＋ 添加子任务
          </button>
        </div>
        <div className="ta-template-steps">
          {draft.subtasks.map((s, i) => (
            <div key={s.id} className="ta-template-step">
              <span className="ta-index">{String(i + 1).padStart(2, "0")}</span>
              <input
                aria-label={`子任务 ${i + 1} 名称`}
                value={s.name}
                placeholder="例如：抓取杯子"
                onChange={(e) =>
                  setDraft((d) => ({
                    ...d,
                    subtasks: d.subtasks.map((step) =>
                      step.id === s.id
                        ? { ...step, name: e.target.value }
                        : step,
                    ),
                  }))
                }
              />
              <button
                type="button"
                aria-label={`上移子任务 ${i + 1}`}
                disabled={i === 0}
                onClick={() =>
                  setDraft((d) => {
                    const list = [...d.subtasks];
                    [list[i - 1], list[i]] = [list[i], list[i - 1]];
                    return { ...d, subtasks: list };
                  })
                }
              >
                ↑
              </button>
              <button
                type="button"
                aria-label={`删除子任务 ${i + 1}`}
                onClick={() =>
                  setDraft((d) => ({
                    ...d,
                    subtasks: d.subtasks.filter((step) => step.id !== s.id),
                  }))
                }
              >
                移除
              </button>
            </div>
          ))}
        </div>
        <div className="ta-section-head">
          <h3>自定义字段</h3>
          <button
            type="button"
            onClick={() =>
              setDraft((d) => ({
                ...d,
                fields: [
                  ...d.fields,
                  { id: uid(), label: "", scope: "attempt", kind: "textarea" },
                ],
              }))
            }
          >
            ＋ 添加字段
          </button>
        </div>
        <div className="ta-template-fields">
          {draft.fields.map((f, i) => (
            <div className="ta-template-field" key={f.id}>
              <label>
                字段名称
                <input
                  aria-label={`字段 ${i + 1} 名称`}
                  value={f.label}
                  onChange={(e) => setField(f.id, { label: e.target.value })}
                  placeholder="例如：操作流畅度"
                />
              </label>
              <label>
                作用范围
                <select
                  aria-label={`字段 ${i + 1} 作用范围`}
                  value={f.scope}
                  onChange={(e) =>
                    setField(f.id, { scope: e.target.value as Scope })
                  }
                >
                  {Object.entries(scopes).map(([v, l]) => (
                    <option key={v} value={v}>
                      {l}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                填写方式
                <select
                  aria-label={`字段 ${i + 1} 填写方式`}
                  value={f.kind}
                  onChange={(e) =>
                    setField(f.id, {
                      kind: e.target.value as Field["kind"],
                      options: f.options || ["选项一", "选项二"],
                    })
                  }
                >
                  {Object.entries(kinds).map(([v, l]) => (
                    <option key={v} value={v}>
                      {l}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                aria-label={`移除字段 ${i + 1}`}
                onClick={() =>
                  setDraft((d) => ({
                    ...d,
                    fields: d.fields.filter((field) => field.id !== f.id),
                  }))
                }
              >
                移除
              </button>
              {["single", "multi"].includes(f.kind) && (
                <label className="ta-option-config">
                  选项（用逗号分隔）
                  <input
                    aria-label={`字段 ${i + 1} 选项`}
                    value={(f.options || []).join("，")}
                    onChange={(e) =>
                      setField(f.id, { options: e.target.value.split(/[,，]/) })
                    }
                  />
                </label>
              )}
            </div>
          ))}
        </div>
        <p className="ta-muted">
          移除定义不会删除旧记录中的值；已有片段会保留创建时的子任务名称。
        </p>
        {(error || serverError) && (
          <p role="alert" className="ta-error">
            {error || serverError}
          </p>
        )}
        <footer className="ta-dialog-foot">
          <button type="button" onClick={onClose}>
            取消
          </button>
          <button className="ta-primary" type="submit" disabled={busy}>
            {busy ? "正在保存…" : "保存任务模板"}
          </button>
        </footer>
      </form>
    </dialog>
  );
}

function Workspace({
  template,
  dataset,
  episode,
  serverDocument,
  onDocumentChange,
  serverDirty = false,
}: {
  template: TaskTemplate;
  dataset: string;
  episode: number;
  serverDocument?: TaskDocument;
  onDocumentChange?: (fn: (doc: TaskDocument) => TaskDocument) => void;
  serverDirty?: boolean;
}) {
  const { frameTimestamps } = useAnnotations();
  const managed = !!serverDocument;
  const { currentTime, seek, duration, setIsPlaying } = useTime();
  const count = frameTimestamps.length;
  const key = documentKey(dataset, episode, template.id);
  const [initial] = useState(() => {
    try {
      if (serverDocument) return { doc: serverDocument, raw: null, error: "" };
      const raw = localStorage.getItem(key);
      return {
        doc: raw ? readDocument(raw, template, count) : emptyDocument(template),
        raw,
        error: "",
      };
    } catch (e) {
      return { doc: emptyDocument(template), raw: null, error: message(e) };
    }
  });
  const [localDoc, setDoc] = useState(initial.doc);
  const doc = serverDocument || localDoc;
  const [storageError, setStorageError] = useState(initial.error);
  const [saved, setSaved] = useState(true);
  const [changed, setChanged] = useState(false);
  const expectedRaw = useRef(initial.raw);
  const [scope, setScope] = useState<Scope>("attempt");
  const [step, setStep] = useState(template.subtasks[0]?.id || "");
  const [selection, setSelection] = useState<string | null>(null);
  const [frameSelection, setFrameSelection] = useState<string | null>(null);
  const [start, setStart] = useState("0"),
    [end, setEnd] = useState("");
  const [pendingValues, setPendingValues] = useState<Record<string, Value>>({});
  const [pendingNullNote, setPendingNullNote] = useState("");
  const [error, setError] = useState("");
  const [menu, setMenu] = useState<{
    track: string;
    id: string;
    anchor: SegmentMenuAnchor;
  } | null>(null);
  const [deleted, setDeleted] = useState<{
    attempt?: Attempt;
    frame?: FrameNote;
    nullSegment?: NullSegment;
  } | null>(null);
  const frame = frameAt(frameTimestamps, currentTime);
  const selected = doc.attempts.find((a) => a.id === selection);
  const selectedNull = doc.nullSegments?.find((a) => a.id === selection);
  const isNull = step === NULL_TRACK_ID;
  const selectedFrame = doc.frames.find((f) => f.id === frameSelection);
  const selectedStep = template.subtasks.find((s) => s.id === step);
  const update = (fn: (d: TaskDocument) => TaskDocument) => {
    if (onDocumentChange) {
      onDocumentChange((d) => ({ ...fn(d), templateSnapshot: template }));
      return;
    }
    setDoc((d) => ({ ...fn(d), templateSnapshot: template }));
    setChanged(true);
    setSaved(false);
  };
  useEffect(() => {
    if (managed || !changed || storageError) return;
    try {
      if (localStorage.getItem(key) !== expectedRaw.current)
        throw new Error(
          "另一窗口修改了这条标注；当前编辑已保留在页面中，请先核对后刷新。",
        );
      const raw = JSON.stringify(doc);
      localStorage.setItem(key, raw);
      expectedRaw.current = raw;
      setSaved(true);
    } catch (e) {
      setStorageError(`浏览器保存失败：${message(e)}`);
      setSaved(false);
    }
  }, [doc, key, changed, storageError, managed]);
  useEffect(() => {
    if (managed) return;
    const listener = (e: StorageEvent) => {
      if (e.key === key && e.newValue !== expectedRaw.current)
        setStorageError("另一窗口修改了这条标注，请核对当前内容后刷新页面。");
    };
    window.addEventListener("storage", listener);
    return () => window.removeEventListener("storage", listener);
  }, [key, managed]);
  const previewTime = (time: number) => {
    setIsPlaying(false);
    seek(Math.max(0, Math.min(duration, time)));
  };
  const jump = (f: number) =>
    previewTime(boundaryTime(frameTimestamps, f, duration));
  const selectAttempt = (a: Attempt, time?: number) => {
    setScope("attempt");
    setStep(a.subtaskId);
    setSelection(a.id);
    setStart(String(a.start));
    setEnd(String(a.end));
    setError("");
    if (time === undefined) jump(a.start);
    else previewTime(time);
  };
  const selectNull = (a: NullSegment, time?: number) => {
    setScope("attempt");
    setStep(NULL_TRACK_ID);
    setSelection(a.id);
    setStart(String(a.start));
    setEnd(String(a.end));
    setError("");
    if (time === undefined) jump(a.start);
    else previewTime(time);
  };
  const newAttempt = (id = step) => {
    setScope("attempt");
    setStep(id);
    setSelection(null);
    setStart(String(frame));
    setEnd("");
    setPendingValues({});
    setPendingNullNote("");
    setError("");
  };
  const selectRecord = (track: string, id: string, time?: number) => {
    if (track === NULL_TRACK_ID) {
      const record = doc.nullSegments?.find((a) => a.id === id);
      if (record) selectNull(record, time);
    } else {
      const record = doc.attempts.find((a) => a.id === id);
      if (record) selectAttempt(record, time);
    }
  };
  const openMenu = (track: string, id: string, anchor: SegmentMenuAnchor) => {
    setIsPlaying(false);
    setMenu({ track, id, anchor });
  };
  const deleteSegment = (track: string, id: string) => {
    const result = deleteTimelineSegment(doc, track, id);
    if (!result.removed) {
      setMenu(null);
      return;
    }
    setDeleted(result.removed);
    update((d) => deleteTimelineSegment(d, track, id).doc);
    if (selection === id) newAttempt(track);
    setMenu(null);
  };
  const commitRange = (
    track: string,
    id: string | null,
    range: { start: number; end: number },
    fromEditor = false,
  ) => {
    const background = track === NULL_TRACK_ID;
    const task = template.subtasks.find((s) => s.id === track);
    const original = id ? doc.attempts.find((a) => a.id === id) : undefined;
    if (!background && !task && !original) {
      setError("请先在任务模板中添加并选择一个子任务");
      return;
    }
    const invalid = validateRange(range.start, range.end, count);
    if (invalid) {
      setError(invalid);
      return;
    }
    if (
      managed &&
      doc.attempts.some(
        (a) => a.id !== id && range.start < a.end && range.end > a.start,
      )
    ) {
      setError("Subtask 区间不能重叠，请调整范围后再标注。");
      return;
    }
    setScope("attempt");
    if (conflictsWithNull(doc, range, background, id)) {
      setError(
        background
          ? "NULL 片段不能与已标注的子任务或其他 NULL 片段重叠，请调整范围。"
          : "这个范围与 NULL 片段重叠，请先调整或移除该 NULL 片段。",
      );
      return;
    }
    const nextId = id || uid();
    if (background)
      update((d) => ({
        ...d,
        nullSegments: id
          ? (d.nullSegments || []).map((a) =>
              a.id === id ? { ...a, ...range } : a,
            )
          : [
              ...(d.nullSegments || []),
              {
                id: nextId,
                label: "NULL",
                ...range,
                note: fromEditor ? pendingNullNote : "",
              },
            ],
      }));
    else
      update((d) => ({
        ...d,
        attempts: id
          ? d.attempts.map((a) => (a.id === id ? { ...a, ...range } : a))
          : [
              ...d.attempts,
              {
                id: nextId,
                subtaskId: track,
                subtaskName: task!.name,
                ...range,
                values: fromEditor ? pendingValues : {},
              },
            ],
      }));
    setStep(track);
    setSelection(nextId);
    setStart(String(range.start));
    setEnd(String(range.end));
    setError("");
  };
  const saveAttempt = () =>
    commitRange(
      step,
      selection,
      {
        start: start === "" ? NaN : Number(start),
        end: end === "" ? NaN : Number(end),
      },
      true,
    );
  const addFrame = () => {
    const existing = doc.frames.find((f) => f.frame === frame);
    if (existing) {
      setFrameSelection(existing.id);
      return;
    }
    const note: FrameNote = { id: uid(), frame, values: {} };
    update((d) => ({ ...d, frames: [...d.frames, note] }));
    setFrameSelection(note.id);
  };
  const records = [
    ...doc.attempts.map((a) => ({
      id: a.id,
      start: a.start,
      end: a.end,
      name:
        template.subtasks.find((s) => s.id === a.subtaskId)?.name ||
        a.subtaskName,
      track: a.subtaskId,
      background: false,
    })),
    ...(doc.nullSegments || []).map((a) => ({
      id: a.id,
      start: a.start,
      end: a.end,
      name: "NULL · 无任务片段",
      track: NULL_TRACK_ID,
      background: true,
    })),
  ].sort((a, b) => a.start - b.start);
  return (
    <>
      <div className="ta-context-strip">
        <span>
          Episode <strong>{String(episode).padStart(3, "0")}</strong> · {count}{" "}
          帧
        </span>
        <span className="ta-save-state" role="status">
          {managed
            ? serverDirty
              ? "● 尚未保存到服务器"
              : "● 服务端标注已加载"
            : storageError
              ? "● 保存需处理"
              : changed && !saved
                ? "正在保存…"
                : "● 已保存在此浏览器"}
        </span>
      </div>
      {storageError && (
        <p role="alert" className="ta-error">
          {storageError}
        </p>
      )}
      <div className="ta-timeline">
        <div className="ta-section-head">
          <div>
            <h3>Subtask 时间轴</h3>
            <p className="ta-muted">
              普通拖动预览 · Shift＋空白拖动新建 · Shift＋片段或边缘拖动调整 ·
              右键删除
            </p>
          </div>
          <span className="ta-time">
            {clock(currentTime)} <span>/ {clock(duration)}</span>
          </span>
        </div>
        <div className="ta-ruler">
          <span>子任务 / 尝试</span>
          <div>
            {[0, 0.25, 0.5, 0.75, 1].map((v) => (
              <span
                key={v}
                style={{
                  left: `${v * 100}%`,
                  transform:
                    v === 0
                      ? "none"
                      : v === 1
                        ? "translateX(-100%)"
                        : "translateX(-50%)",
                }}
              >
                {clock(duration * v)}
              </span>
            ))}
          </div>
        </div>
        <TaskAnnotationTimeline
          tracks={[
            ...template.subtasks.map((s) => ({
              id: s.id,
              name: s.name,
              segments: doc.attempts
                .filter((a) => a.subtaskId === s.id)
                .map((a) => {
                  const n = attemptNumber(doc.attempts, a.id);
                  const result = template.fields.find(
                    (f) => f.scope === "attempt" && f.kind === "single",
                  );
                  const outcome = result ? a.values[result.id] : null;
                  return {
                    id: a.id,
                    start: a.start,
                    end: a.end,
                    label: `第 ${n} 次${outcome ? ` · ${outcome}` : ""}`,
                    description: `${s.name} 第 ${n} 次尝试`,
                  };
                }),
            })),
            {
              id: NULL_TRACK_ID,
              name: "NULL · 无任务片段",
              background: true,
              segments: (doc.nullSegments || []).map((a) => ({
                id: a.id,
                start: a.start,
                end: a.end,
                label: "NULL",
                description: `NULL 片段，帧 ${a.start}–${a.end}`,
              })),
            },
          ]}
          timestamps={frameTimestamps}
          duration={duration}
          currentTime={currentTime}
          activeTrack={scope === "attempt" ? step : ""}
          selectedId={scope === "attempt" ? selection : null}
          onNew={newAttempt}
          onSelect={selectRecord}
          onCommit={commitRange}
          onSeek={previewTime}
          onPause={() => setIsPlaying(false)}
          onMenu={openMenu}
        />
        <p className="ta-null-hint">
          NULL 表示已确认没有任务内容；时间轴留空仍表示未标注。
        </p>
        {!template.subtasks.length && (
          <p className="ta-empty">还没有子任务，请先配置任务模板。</p>
        )}
        {!!doc.attempts.filter(
          (a) => !template.subtasks.some((s) => s.id === a.subtaskId),
        ).length && (
          <div className="ta-archived">
            <span>模板中已移除的子任务：</span>
            {doc.attempts
              .filter(
                (a) => !template.subtasks.some((s) => s.id === a.subtaskId),
              )
              .map((a) => (
                <button key={a.id} onClick={() => selectAttempt(a)}>
                  {a.subtaskName} · 帧 {a.start}–{a.end}
                </button>
              ))}
          </div>
        )}
      </div>
      <div className="ta-scope-tabs" role="group" aria-label="标注作用范围">
        {(["episode", "attempt", "frame"] as Scope[]).map((s) => (
          <button
            key={s}
            aria-pressed={scope === s}
            className={scope === s ? "active" : ""}
            onClick={() => {
              setScope(s);
              setError("");
            }}
          >
            <span>{s === "episode" ? "◫" : s === "attempt" ? "↔" : "⌖"}</span>
            {scopes[s]}
            {s === "attempt" ? (
              <small>{records.length}</small>
            ) : s === "frame" ? (
              <small>{doc.frames.length}</small>
            ) : null}
          </button>
        ))}
      </div>
      {scope === "episode" && (
        <section className="ta-editor">
          <div className="ta-editor-title">
            <div>
              <span className="ta-kicker">EPISODE ATTRIBUTES</span>
              <h3>整条任务的评价</h3>
            </div>
            <span className="ta-badge">作用于整个 Episode</span>
          </div>
          <Fields
            template={template}
            scope="episode"
            values={doc.values}
            onChange={(values) => update((d) => ({ ...d, values }))}
          />
        </section>
      )}
      {scope === "attempt" && (
        <div className="ta-attempt-layout">
          <aside className="ta-attempt-list">
            <div className="ta-section-head">
              <h3>片段记录</h3>
              <button onClick={() => newAttempt()}>＋ 新片段</button>
            </div>
            {records.map((a) => (
              <button
                key={a.id}
                className={`ta-attempt-item ${a.background ? "ta-null-item" : ""} ${selection === a.id ? "active" : ""}`}
                onClick={() => selectRecord(a.track, a.id)}
                onContextMenu={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  openMenu(
                    a.track,
                    a.id,
                    segmentMenuAnchor(e.currentTarget, e.clientX, e.clientY),
                  );
                }}
                onKeyDown={(e) => {
                  if (
                    e.key === "ContextMenu" ||
                    (e.shiftKey && e.key === "F10")
                  ) {
                    e.preventDefault();
                    e.stopPropagation();
                    openMenu(a.track, a.id, segmentMenuAnchor(e.currentTarget));
                  }
                }}
              >
                <span>
                  <strong>{a.name}</strong>
                  <small>
                    {a.background
                      ? "已确认"
                      : `第 ${attemptNumber(doc.attempts, a.id)} 次`}
                  </small>
                </span>
                <span className="ta-muted">
                  {clock(boundaryTime(frameTimestamps, a.start, duration))} —{" "}
                  {clock(boundaryTime(frameTimestamps, a.end, duration))}
                </span>
              </button>
            ))}
            {!records.length && (
              <p className="ta-empty">
                还没有片段。按住 Shift 在时间轴拖出一段，或在这里填写起止位置。
              </p>
            )}
          </aside>
          <section className="ta-editor">
            <div className="ta-editor-title">
              <div>
                <span className="ta-kicker">
                  {isNull ? "NULL / BACKGROUND" : "SUBTASK ATTEMPT"}
                </span>
                <h3>
                  {isNull
                    ? selectedNull
                      ? "NULL · 无任务片段"
                      : "记录一段无任务内容"
                    : selected
                      ? `${template.subtasks.find((s) => s.id === selected.subtaskId)?.name || selected.subtaskName} · 第 ${attemptNumber(doc.attempts, selected.id)} 次尝试`
                      : "记录一次新尝试"}
                </h3>
              </div>
              {(selected || selectedNull) && (
                <button
                  className="ta-danger"
                  onClick={() =>
                    deleteSegment(
                      selectedNull ? NULL_TRACK_ID : selected!.subtaskId,
                      (selectedNull || selected)!.id,
                    )
                  }
                >
                  删除片段
                </button>
              )}
            </div>
            {!count && (
              <p role="alert" className="ta-error">
                此数据没有可用的源帧时间戳，暂不能标记片段和单帧。
              </p>
            )}
            {!selected && !selectedNull && (
              <label className="ta-field">
                片段类型
                <select
                  aria-label="所属子任务"
                  value={step}
                  onChange={(e) => setStep(e.target.value)}
                >
                  <option value="" disabled>
                    选择子任务
                  </option>
                  {template.subtasks.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                  <option value={NULL_TRACK_ID}>NULL · 无任务片段</option>
                </select>
              </label>
            )}
            <div className="ta-boundaries">
              <label>
                起始帧（含）
                <div>
                  <input
                    type="number"
                    aria-label="起始帧"
                    min={0}
                    max={count - 1}
                    value={start}
                    onChange={(e) => setStart(e.target.value)}
                  />
                  <button
                    disabled={!count}
                    onClick={() => setStart(String(frame))}
                  >
                    标记起点
                  </button>
                </div>
              </label>
              <label>
                结束边界（不含）
                <div>
                  <input
                    type="number"
                    aria-label="结束边界"
                    min={1}
                    max={count}
                    value={end}
                    placeholder="待标记"
                    onChange={(e) => setEnd(e.target.value)}
                  />
                  <button
                    disabled={!count}
                    onClick={() =>
                      setEnd(
                        String(
                          frameAt(frameTimestamps, currentTime, true, duration),
                        ),
                      )
                    }
                  >
                    标记终点
                  </button>
                </div>
              </label>
            </div>
            <p className="ta-muted ta-range-hint">
              当前帧 <strong>{frame}</strong> · 最后一帧为{" "}
              {Math.max(count - 1, 0)}，覆盖到片尾时结束边界填 {count}。
            </p>
            {isNull ? (
              <label className="ta-field">
                标记说明（可选）
                <textarea
                  aria-label="NULL 标记说明"
                  rows={3}
                  value={selectedNull?.note ?? pendingNullNote}
                  placeholder="例如：等待开始、无关操作、任务结束后的空闲时间…"
                  onChange={(e) =>
                    selectedNull
                      ? update((d) => ({
                          ...d,
                          nullSegments: (d.nullSegments || []).map((a) =>
                            a.id === selectedNull.id
                              ? { ...a, note: e.target.value }
                              : a,
                          ),
                        }))
                      : setPendingNullNote(e.target.value)
                  }
                />
              </label>
            ) : (
              <Fields
                template={template}
                scope="attempt"
                values={selected?.values || pendingValues}
                onChange={(values) =>
                  selected
                    ? update((d) => ({
                        ...d,
                        attempts: d.attempts.map((a) =>
                          a.id === selected.id ? { ...a, values } : a,
                        ),
                      }))
                    : setPendingValues(values)
                }
              />
            )}
            {error && (
              <p role="alert" className="ta-error">
                {error}
              </p>
            )}
            <div className="ta-editor-foot">
              <span className="ta-muted">
                {selected || selectedNull
                  ? "Shift＋拖动调整并保存；手动输入边界后点击更新。"
                  : "Shift＋拖动新建并保存；普通拖动只预览视频。"}
              </span>
              <button
                className="ta-primary"
                disabled={!count || (!selected && !selectedStep && !isNull)}
                onClick={saveAttempt}
              >
                {selected || selectedNull
                  ? "更新片段边界"
                  : isNull
                    ? "添加 NULL 片段"
                    : "添加这次尝试"}
              </button>
            </div>
          </section>
        </div>
      )}
      {scope === "frame" && (
        <div className="ta-attempt-layout">
          <aside className="ta-attempt-list">
            <div className="ta-section-head">
              <h3>单帧记录</h3>
            </div>
            {[...doc.frames]
              .sort((a, b) => a.frame - b.frame)
              .map((f) => (
                <button
                  key={f.id}
                  className={`ta-attempt-item ${frameSelection === f.id ? "active" : ""}`}
                  onClick={() => {
                    setFrameSelection(f.id);
                    jump(f.frame);
                  }}
                >
                  <strong>帧 {f.frame}</strong>
                  <span className="ta-muted">
                    {clock(boundaryTime(frameTimestamps, f.frame, duration))}
                  </span>
                </button>
              ))}
            {!doc.frames.length && (
              <p className="ta-empty">
                把视频定位到需要记录的一帧，再添加标注。
              </p>
            )}
          </aside>
          <section className="ta-editor">
            <div className="ta-editor-title">
              <div>
                <span className="ta-kicker">FRAME ANNOTATION</span>
                <h3>
                  {selectedFrame
                    ? `帧 ${selectedFrame.frame} 的观察`
                    : `当前帧 ${frame}`}
                </h3>
              </div>
              <button
                disabled={!count}
                className="ta-primary"
                onClick={addFrame}
              >
                ＋ 标注当前帧
              </button>
            </div>
            {selectedFrame ? (
              <>
                <p className="ta-muted">
                  记录固定在帧 {selectedFrame.frame}，播放视频不会移动这条记录。
                </p>
                <Fields
                  template={template}
                  scope="frame"
                  values={selectedFrame.values}
                  onChange={(values) =>
                    update((d) => ({
                      ...d,
                      frames: d.frames.map((f) =>
                        f.id === selectedFrame.id ? { ...f, values } : f,
                      ),
                    }))
                  }
                />
                <button
                  className="ta-danger"
                  onClick={() => {
                    setDeleted({ frame: selectedFrame });
                    update((d) => ({
                      ...d,
                      frames: d.frames.filter((f) => f.id !== selectedFrame.id),
                    }));
                    setFrameSelection(null);
                  }}
                >
                  删除这条单帧标注
                </button>
              </>
            ) : (
              <p className="ta-empty">
                单帧标注适合记录打滑、碰撞、遮挡等具体时刻的观察。
              </p>
            )}
          </section>
        </div>
      )}
      {deleted && (
        <div className="ta-undo" role="status">
          已删除记录
          <button
            onClick={() => {
              if (
                deleted.nullSegment &&
                conflictsWithNull(doc, deleted.nullSegment, true)
              ) {
                setError("原位置已有片段，无法恢复 NULL 记录，请先调整范围。");
                return;
              }
              if (
                deleted.attempt &&
                (conflictsWithNull(doc, deleted.attempt, false) ||
                  (managed &&
                    doc.attempts.some(
                      (a) =>
                        deleted.attempt!.start < a.end &&
                        deleted.attempt!.end > a.start,
                    )))
              ) {
                setError(
                  "原位置已有 NULL 片段，无法恢复子任务，请先调整范围。",
                );
                return;
              }
              update((d) => ({
                ...d,
                attempts: deleted.attempt
                  ? [...d.attempts, deleted.attempt]
                  : d.attempts,
                frames: deleted.frame ? [...d.frames, deleted.frame] : d.frames,
                nullSegments: deleted.nullSegment
                  ? [...(d.nullSegments || []), deleted.nullSegment]
                  : d.nullSegments,
              }));
              setDeleted(null);
            }}
          >
            撤销删除
          </button>
          <button aria-label="关闭删除提示" onClick={() => setDeleted(null)}>
            ×
          </button>
        </div>
      )}
      {menu &&
        records.some((a) => a.id === menu.id && a.track === menu.track) && (
          <SegmentContextMenu
            anchor={menu.anchor}
            label={
              records.find((a) => a.id === menu.id)!.name +
              (menu.track === NULL_TRACK_ID
                ? ""
                : ` · 第 ${attemptNumber(doc.attempts, menu.id)} 次尝试`)
            }
            onDelete={() => deleteSegment(menu.track, menu.id)}
            onClose={() => setMenu(null)}
          />
        )}
    </>
  );
}

function BrowserStudio() {
  const { episodeId, ident } = useAnnotations();
  const dataset = ident.localPath || ident.repoId || "";
  const [templates, setTemplates] = useState<TaskTemplate[]>([]);
  const [active, setActive] = useState("");
  const [editing, setEditing] = useState<TaskTemplate | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!dataset) return;
    try {
      const raw = localStorage.getItem(TEMPLATE_KEY);
      const loaded: TaskTemplate[] = raw
        ? JSON.parse(raw)
        : [defaultTemplate()];
      if (
        !Array.isArray(loaded) ||
        !loaded.length ||
        new Set(loaded.map((t) => t.id)).size !== loaded.length ||
        loaded.some((t) => validateTemplate(t))
      )
        throw new Error("浏览器中的任务模板格式不正确，原内容已保留");
      setTemplates(loaded);
      setActive(localStorage.getItem(bindingKey(dataset)) || loaded[0].id);
    } catch (e) {
      setError(message(e));
    }
  }, [dataset]);
  const template = templates.find((t) => t.id === active) || templates[0];
  const bind = (id: string) => {
    try {
      localStorage.setItem(bindingKey(dataset), id);
      setActive(id);
    } catch (e) {
      setError(`模板选择未保存：${message(e)}`);
    }
  };
  const saveTemplate = (next: TaskTemplate) => {
    const list = templates.some((t) => t.id === next.id)
      ? templates.map((t) => (t.id === next.id ? next : t))
      : [...templates, next];
    try {
      localStorage.setItem(TEMPLATE_KEY, JSON.stringify(list));
      localStorage.setItem(bindingKey(dataset), next.id);
      setTemplates(list);
      setActive(next.id);
      setEditing(null);
      setError("");
    } catch (e) {
      setError(`模板保存失败：${message(e)}`);
    }
  };
  return (
    <section className="ta-studio" aria-label="任务模板标注工作区">
      <header className="ta-header">
        <div>
          <span className="ta-kicker">TASK ANNOTATION</span>
          <h2>任务标注</h2>
          <p>一套任务模板，多条数据复用；每次执行尝试独立记录。</p>
        </div>
        <div className="ta-template-picker">
          <label>
            任务模板
            <select
              aria-label="当前任务模板"
              value={template?.id || ""}
              onChange={(e) => bind(e.target.value)}
            >
              {templates.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
          <button disabled={!template} onClick={() => setEditing(template)}>
            管理任务模板
          </button>
          <button
            onClick={() =>
              setEditing({
                ...defaultTemplate(),
                id: uid(),
                name: "新任务模板",
                subtasks: [],
              })
            }
          >
            ＋ 新模板
          </button>
        </div>
      </header>
      {error && (
        <p role="alert" className="ta-error">
          {error}
        </p>
      )}
      <p className="ta-local-note">
        浏览器本地保存 · 仅当前浏览器可见，清理站点数据会清除记录。
      </p>
      {template && episodeId != null && dataset ? (
        <Workspace
          key={`${dataset}:${episodeId}:${template.id}`}
          template={template}
          dataset={dataset}
          episode={episodeId}
        />
      ) : (
        !error && <p className="ta-muted">正在读取任务模板…</p>
      )}
      {editing && (
        <TemplateEditor
          initial={editing}
          onClose={() => setEditing(null)}
          onSave={saveTemplate}
        />
      )}
    </section>
  );
}

function ManagedStudio({
  dataset,
  episode,
}: {
  dataset: string;
  episode: number;
}) {
  const {
    taskDocument: doc,
    updateTaskDocument,
    reviewLoading,
    reviewReady,
    reviewError,
    reviewRevision,
    reviewHasDraft,
    reviewExporting,
    frameTimestamps,
    save,
    saving,
    dirty,
    exportReview,
  } = useAnnotations();
  const [library, setLibrary] = useState<TemplateLibrary | null>(null);
  const [editing, setEditing] = useState<TaskTemplate | null>(null);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [templateBusy, setTemplateBusy] = useState(false);
  const [browserDraft, setBrowserDraft] = useState<TaskDocument | null>(null);
  const appliedLibrary = useRef("");
  useEffect(() => {
    if (!doc || !library) return;
    const key = `${library.revision}:${doc.templateId}`;
    if (appliedLibrary.current === key) return;
    appliedLibrary.current = key;
    const shared = library.templates.find((t) => t.id === doc.templateId);
    if (!shared) return;
    try {
      const next = retargetDocument(doc, shared);
      if (JSON.stringify(next) !== JSON.stringify(doc)) {
        updateTaskDocument(next);
        setStatus(
          "已应用共用模板的最新字段，旧记录保留；请核对后保存 Episode。",
        );
      }
    } catch (e) {
      setError(`共用模板未套用：${message(e)}`);
    }
  }, [library, doc, updateTaskDocument]);
  useEffect(() => {
    let alive = true;
    reviewRequest<TemplateLibrary>("/api/task-templates")
      .then((value) => {
        if (alive) setLibrary(value);
      })
      .catch((e) => {
        if (alive) setError(`模板库读取失败：${message(e)}`);
      });
    return () => {
      alive = false;
    };
  }, [dataset]);
  useEffect(() => {
    try {
      const templateId = localStorage.getItem(bindingKey(dataset));
      const raw = templateId
        ? localStorage.getItem(documentKey(dataset, episode, templateId))
        : null;
      if (raw) {
        const candidate = JSON.parse(raw) as TaskDocument;
        if (candidate?.version === 1) setBrowserDraft(candidate);
      }
    } catch {
      /* The original browser records are left untouched. */
    }
  }, [dataset, episode]);
  const update = (fn: (doc: TaskDocument) => TaskDocument) =>
    updateTaskDocument((d) => (d ? fn(d) : d));
  const templates = [...(library?.templates || [])];
  if (doc && !templates.some((t) => t.id === doc.templateId))
    templates.push(doc.templateSnapshot);
  const choose = (template: TaskTemplate) => {
    try {
      if (doc) updateTaskDocument(retargetDocument(doc, template));
      setError("");
    } catch (e) {
      setError(message(e));
    }
  };
  const saveTemplate = async (next: TaskTemplate) => {
    if (!library) return;
    setTemplateBusy(true);
    setError("");
    try {
      const list = templates.some((t) => t.id === next.id)
        ? templates.map((t) => (t.id === next.id ? next : t))
        : [...templates, next];
      const result = await reviewRequest<TemplateLibrary>(
        "/api/task-templates",
        { revision: library.revision, templates: list },
      );
      setLibrary(result);
      choose(next);
      setEditing(null);
      setStatus("任务模板已保存到服务器；当前 Episode 的修改仍需保存。");
    } catch (e) {
      setError(message(e));
    } finally {
      setTemplateBusy(false);
    }
  };
  const saveServer = async () => {
    setStatus("");
    setError("");
    const result = await save();
    if (result.ok) setStatus("已保存到服务器。");
    else setError(result.error || "保存失败，当前标注保留在页面中。");
  };
  const exportDataset = async () => {
    setStatus("正在导出修订数据集…");
    setError("");
    try {
      const result = await exportReview();
      setStatus(`导出完成：${result.output_dir}`);
    } catch (e) {
      setError(message(e));
      setStatus("");
    }
  };
  const importBrowser = () => {
    if (!browserDraft || !doc) return;
    try {
      taskSegments(browserDraft, frameTimestamps.length);
    } catch (e) {
      setError(`旧标注无法导入：${message(e)}`);
      return;
    }
    if (
      window.confirm(
        "用这条数据的浏览器旧标注替换当前任务标注？服务端内容在点击保存前保持不变，原浏览器记录保留。",
      )
    ) {
      updateTaskDocument(browserDraft);
      setBrowserDraft(null);
      setStatus("已导入浏览器草稿，请核对后保存到服务器。");
    }
  };
  const busy = saving || reviewExporting || templateBusy;
  return (
    <section className="ta-studio" aria-label="任务模板标注工作区">
      <header className="ta-header">
        <div>
          <span className="ta-kicker">TASK ANNOTATION</span>
          <h2>任务标注</h2>
          <p>
            {reviewHasDraft
              ? `服务端草稿 v${reviewRevision}`
              : "AI / 来源初标 · 请核对片段后保存"}{" "}
            · 模板、评分与备注随标注保存
          </p>
        </div>
        <div className="ta-template-picker">
          <label>
            任务模板
            <select
              aria-label="当前任务模板"
              value={doc?.templateId || ""}
              disabled={!reviewReady || busy || !library}
              onChange={(e) => {
                const t = templates.find((t) => t.id === e.target.value);
                if (t) choose(t);
              }}
            >
              {templates.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </label>
          <button
            disabled={!reviewReady || busy || !library}
            onClick={() => doc && setEditing(doc.templateSnapshot)}
          >
            管理任务模板
          </button>
          <button
            disabled={!reviewReady || busy || !library}
            onClick={() =>
              setEditing({
                ...defaultTemplate(),
                id: uid(),
                name: "新任务模板",
                subtasks: [],
              })
            }
          >
            ＋ 新模板
          </button>
        </div>
      </header>
      <div className="ta-server-actions">
        <button
          className="ta-primary"
          disabled={!reviewReady || busy || (!dirty && reviewHasDraft)}
          onClick={saveServer}
        >
          {saving ? "正在保存…" : "保存到服务器"}
        </button>
        <button
          disabled={!reviewReady || busy || dirty}
          onClick={exportDataset}
        >
          {reviewExporting ? "正在导出…" : "导出修订数据集"}
        </button>
        {browserDraft && (
          <button disabled={!reviewReady || busy} onClick={importBrowser}>
            导入浏览器旧标注
          </button>
        )}
        <span className="ta-muted">
          {dirty ? "有未保存修改" : ""} · 导出生成独立副本
        </span>
      </div>
      {(error || reviewError) && (
        <p className="ta-error" role="alert">
          {error || reviewError}
        </p>
      )}
      {status && (
        <p role="status" className="ta-local-note">
          {status}
        </p>
      )}
      {reviewLoading && <p role="status">正在读取服务端标注…</p>}
      {reviewReady && doc && (
        <Workspace
          key={`${dataset}:${episode}:${doc.templateId}`}
          template={doc.templateSnapshot}
          dataset={dataset}
          episode={episode}
          serverDocument={doc}
          onDocumentChange={update}
          serverDirty={dirty}
        />
      )}
      {editing && (
        <TemplateEditor
          initial={editing}
          serverError={error}
          busy={templateBusy}
          onClose={() => {
            if (!templateBusy) setEditing(null);
          }}
          onSave={(next) => {
            if (!templateBusy) void saveTemplate(next);
          }}
        />
      )}
    </section>
  );
}

export function TaskAnnotationStudio() {
  const { ident, episodeId } = useAnnotations();
  return ident.repoId?.startsWith("bp/") && episodeId != null ? (
    <ManagedStudio
      key={`${ident.repoId}:${episodeId}`}
      dataset={ident.repoId}
      episode={episodeId}
    />
  ) : (
    <BrowserStudio />
  );
}
