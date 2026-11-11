"use client";

/**
 * Manual episode trim: drag out the time ranges to delete, then join the
 * remaining frames into one continuous episode.
 *
 * By default the current dataset is replaced in place: the Server builds and
 * validates a staged copy first, keeps the pre-trim version as a backup, then
 * swaps it into the source path. The Viewer already knows the exact source
 * frame timestamps, so the browser works in frames and the Server
 * re-validates the ranges against the real episode length before anything is
 * written.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { useAnnotations } from "../context/annotations-context";
import { useTime } from "../context/time-context";

type FrameRange = { start: number; end: number };

type TrimJob = {
  id: string;
  state: string;
  stage?: string | null;
  error?: string | null;
  dataset_name?: string | null;
  dataset_ready?: boolean;
  in_place?: boolean;
  backup_path?: string | null;
  manual_summary?: {
    frames?: number;
    frames_removed?: number;
    frames_kept?: number;
  } | null;
};

const TERMINAL = new Set([
  "COMPLETED",
  "CONVERTED",
  "SYNTHETIC_READY",
  "QUARANTINED",
  "FAILED",
  "INTERRUPTED",
  "BLOCKED",
  "DELETED",
]);
const SUCCESS = new Set(["COMPLETED", "CONVERTED", "SYNTHETIC_READY"]);
const POLL_INTERVAL = 2500;

const clamp = (value: number, low: number, high: number) =>
  Math.min(high, Math.max(low, value));

function nearestFrame(timestamps: number[], time: number) {
  if (timestamps.length === 0) return 0;
  let low = 0;
  let high = timestamps.length - 1;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (timestamps[middle] < time) low = middle + 1;
    else high = middle;
  }
  if (low > 0 && Math.abs(timestamps[low - 1] - time) <= Math.abs(timestamps[low] - time)) {
    return low - 1;
  }
  return low;
}

function mergeRanges(values: FrameRange[]) {
  const sorted = [...values].sort((a, b) => a.start - b.start);
  const merged: FrameRange[] = [];
  for (const value of sorted) {
    const previous = merged[merged.length - 1];
    if (previous && value.start <= previous.end) {
      previous.end = Math.max(previous.end, value.end);
    } else {
      merged.push({ ...value });
    }
  }
  return merged;
}

function statusText(job: TrimJob) {
  if (job.state === "COMPLETED" || job.state === "CONVERTED" || job.state === "SYNTHETIC_READY") {
    return job.in_place ? "裁剪完成，原数据集已更新" : "裁剪完成";
  }
  if (job.state === "QUARANTINED") return "裁剪完成，Doctor 发现问题";
  if (job.state === "FAILED") return "裁剪失败";
  if (job.state === "INTERRUPTED") return "裁剪已中断";
  if (job.state === "BLOCKED") return "裁剪被阻止";
  return job.stage || "等待裁剪";
}

const formatSeconds = (value: number) => `${value.toFixed(2)}s`;

// Trim job progress is keyed by dataset + episode so switching episodes (or
// reloading the page) keeps the running/completed status instead of losing it.
const JOB_STORAGE_PREFIX = "bodypipeline.manual-trim.job:";

function jobStorageKey(datasetId: string, episodeId: number) {
  return `${JOB_STORAGE_PREFIX}${datasetId}:${episodeId}`;
}

function readStoredJob(datasetId: string, episodeId: number): TrimJob | null {
  try {
    const raw = window.sessionStorage.getItem(jobStorageKey(datasetId, episodeId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as TrimJob;
    if (!parsed || typeof parsed.id !== "string" || typeof parsed.state !== "string") {
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

function writeStoredJob(datasetId: string, episodeId: number, job: TrimJob | null) {
  try {
    const key = jobStorageKey(datasetId, episodeId);
    if (job) window.sessionStorage.setItem(key, JSON.stringify(job));
    else window.sessionStorage.removeItem(key);
  } catch {
    // Private mode or quota: progress simply will not survive a reload.
  }
}

export function ManualTrimPanel() {
  const { episodeId, ident, frameTimestamps } = useAnnotations();
  const { currentTime, duration, seek } = useTime();
  const repoId = ident.repoId || "";
  const managed = repoId.startsWith("bp/");
  const datasetId = managed ? repoId.slice(3) : "";

  const frames = frameTimestamps.length > 0 ? frameTimestamps.length : 0;
  const fps = useMemo(() => {
    if (frameTimestamps.length > 1) {
      const span = frameTimestamps[frameTimestamps.length - 1] - frameTimestamps[0];
      if (span > 0) return (frameTimestamps.length - 1) / span;
    }
    return 30;
  }, [frameTimestamps]);
  const frameCount = frames > 0 ? frames : Math.max(1, Math.round(duration * fps));

  const [ranges, setRanges] = useState<FrameRange[]>([]);
  const [draft, setDraft] = useState<FrameRange | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [job, setJob] = useState<TrimJob | null>(null);
  const [jobKey, setJobKey] = useState("");
  const currentKey =
    datasetId && episodeId != null ? jobStorageKey(datasetId, episodeId) : "";
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const trackRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ anchor: number; current: number } | null>(null);
  // Only reload after a job we actually watched finishes. A job restored from
  // sessionStorage is already reflected by the freshly loaded page.
  const watchedRef = useRef<Set<string>>(new Set());
  const reloadedRef = useRef<Set<string>>(new Set());

  const frameToTime = useCallback(
    (frame: number) => {
      if (frameTimestamps.length > 0) {
        const index = clamp(Math.round(frame), 0, frameTimestamps.length - 1);
        return frameTimestamps[index];
      }
      return clamp(frame, 0, frameCount) / fps;
    },
    [frameTimestamps, frameCount, fps],
  );

  const timeToFrame = useCallback(
    (time: number) => {
      if (frameTimestamps.length > 0) {
        return clamp(nearestFrame(frameTimestamps, time), 0, frameTimestamps.length);
      }
      return clamp(Math.round(time * fps), 0, frameCount);
    },
    [frameTimestamps, frameCount, fps],
  );

  // A new source episode invalidates ranges, then restores any trim job that
  // belongs to that episode so its progress survives switching away and back.
  useEffect(() => {
    setRanges([]);
    setDraft(null);
    setSelected(null);
    setError("");
    if (datasetId && episodeId != null) {
      setJobKey(jobStorageKey(datasetId, episodeId));
      setJob(readStoredJob(datasetId, episodeId));
    } else {
      setJobKey("");
      setJob(null);
    }
  }, [repoId, episodeId, datasetId]);

  // Persist only once the state belongs to the episode currently on screen;
  // otherwise a render triggered by an episode switch could write the old job
  // into the new episode's slot.
  useEffect(() => {
    if (!job || !jobKey || jobKey !== currentKey || !datasetId || episodeId == null) return;
    writeStoredJob(datasetId, episodeId, job);
  }, [job, jobKey, currentKey, datasetId, episodeId]);

  useEffect(() => {
    if (!job || TERMINAL.has(job.state) || !datasetId || episodeId == null) return;
    watchedRef.current.add(job.id);
    let active = true;
    const timer = window.setTimeout(async () => {
      try {
        const response = await fetch(
          `/api/manual-trims/${encodeURIComponent(datasetId)}/jobs/${encodeURIComponent(job.id)}`,
          { credentials: "same-origin", cache: "no-store" },
        );
        if (response.status === 404) {
          if (active) {
            writeStoredJob(datasetId, episodeId, null);
            setJob(null);
          }
          return;
        }
        if (!response.ok) return;
        const payload = (await response.json()) as TrimJob;
        if (active) setJob(payload);
      } catch {
        // Transient gateway/Server hiccup: the next tick retries.
      }
    }, POLL_INTERVAL);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [job, datasetId, episodeId]);

  // An in-place trim rewrites the dataset behind the same preview URL, but the
  // viewer already loaded the old timestamps and videos. Reload once after a
  // watched job succeeds so the new duration and frame count appear at once.
  useEffect(() => {
    if (!job?.in_place || !SUCCESS.has(job.state)) return;
    if (!watchedRef.current.has(job.id) || reloadedRef.current.has(job.id)) return;
    reloadedRef.current.add(job.id);
    const timer = window.setTimeout(() => {
      const url = new URL(window.location.href);
      url.searchParams.set("trim", String(Date.now()));
      window.location.replace(url.toString());
    }, 1500);
    return () => window.clearTimeout(timer);
  }, [job]);

  const removedFrames = ranges.reduce((total, range) => total + range.end - range.start, 0);
  const keptFrames = Math.max(0, frameCount - removedFrames);
  const durationSeconds = duration > 0 ? duration : frameToTime(frameCount);
  const playhead = durationSeconds > 0 ? clamp((currentTime / durationSeconds) * 100, 0, 100) : 0;

  const frameAt = useCallback(
    (clientX: number) => {
      const element = trackRef.current;
      if (!element) return 0;
      const rect = element.getBoundingClientRect();
      const ratio = rect.width > 0 ? (clientX - rect.left) / rect.width : 0;
      return clamp(Math.round(ratio * frameCount), 0, frameCount);
    },
    [frameCount],
  );

  const beginSelection = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!managed || submitting || event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    const frame = frameAt(event.clientX);
    dragRef.current = { anchor: frame, current: frame };
    setSelected(null);
    setDraft({ start: frame, end: frame });
    setError("");
  };

  const extendSelection = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag) return;
    const frame = frameAt(event.clientX);
    drag.current = frame;
    setDraft({ start: Math.min(drag.anchor, frame), end: Math.max(drag.anchor, frame) });
  };

  const finishSelection = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    dragRef.current = null;
    setDraft(null);
    if (!drag) return;
    drag.current = frameAt(event.clientX);
    const start = Math.min(drag.anchor, drag.current);
    const end = Math.max(drag.anchor, drag.current);
    if (end - start < 1) {
      seek(frameToTime(start));
      return;
    }
    setRanges((previous) => mergeRanges([...previous, { start, end }]));
  };

  const cancelSelection = () => {
    dragRef.current = null;
    setDraft(null);
  };

  const removeRange = (index: number) => {
    setRanges((previous) => previous.filter((_, position) => position !== index));
    setSelected(null);
  };

  const nudgeSelected = (side: "start" | "end", delta: number) => {
    if (selected == null) return;
    setRanges((previous) => {
      const value = previous[selected];
      if (!value) return previous;
      const next = { ...value };
      if (side === "start") next.start = clamp(next.start + delta, 0, next.end - 1);
      else next.end = clamp(next.end + delta, next.start + 1, frameCount);
      return previous.map((range, index) => (index === selected ? next : range));
    });
  };

  const submit = async () => {
    if (!managed || !datasetId || episodeId == null || submitting) return;
    if (ranges.length === 0) {
      setError("请先在时间轴上拖选要删除的时间段");
      return;
    }
    if (keptFrames < 1) {
      setError("不能删除整个 Episode，请至少保留一帧");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const response = await fetch(
        `/api/manual-trims/${encodeURIComponent(datasetId)}`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            episode_index: episodeId,
            remove_ranges: ranges.map((range) => [range.start, range.end]),
            run_doctor: false,
            in_place: true,
          }),
        },
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload?.detail || "提交裁剪失败");
      const submitted = payload as TrimJob;
      watchedRef.current.add(submitted.id);
      setJob(submitted);
      setJobKey(currentKey);
      setRanges([]);
      setSelected(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSubmitting(false);
    }
  };

  const refreshPreview = () => {
    const url = new URL(window.location.href);
    url.searchParams.set("trim", String(Date.now()));
    window.location.replace(url.toString());
  };

  const openResult = async () => {
    if (!job || !datasetId) return;
    if (job.in_place) {
      // The current preview already points at the rewritten dataset; reload it
      // instead of opening an unrelated episode_0 from the trim task.
      refreshPreview();
      return;
    }
    const response = await fetch(
      `/api/manual-trims/${encodeURIComponent(datasetId)}/jobs/${encodeURIComponent(job.id)}/preview`,
      { method: "POST", credentials: "same-origin" },
    );
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload?.url) {
      setError(payload?.detail || "无法打开裁剪结果");
      return;
    }
    const target = String(payload.url).replace(
      /\/episode_\d+(?=$|[?#])/,
      `/episode_${episodeId}`,
    );
    window.open(target, "_blank", "noopener");
  };

  if (!managed || episodeId == null) return null;

  const busy = submitting || (job != null && !TERMINAL.has(job.state));
  const finished = job != null && TERMINAL.has(job.state) && !!job.dataset_ready;

  return (
    <section className="bp-trim-panel" aria-label="手动裁剪时间段">
      <div className="bp-trim-heading">
        <div>
          <strong>手动裁剪时间段</strong>
          <span className="bp-trim-sub">
            Episode {episodeId} · {frameCount} 帧 · 直接修改当前数据集，保留片段前后直接相连
          </span>
          <span className="bp-trim-sub">
            删除会替换当前数据集；裁剪前的版本会在任务目录保留备份。
          </span>
        </div>
        <span className="bp-trim-count">
          删除 {removedFrames} 帧 / 保留 {keptFrames} 帧
        </span>
      </div>

      <div
        ref={trackRef}
        className="bp-trim-track"
        role="slider"
        aria-label="拖选要删除的时间段"
        aria-valuemin={0}
        aria-valuemax={frameCount}
        aria-valuenow={timeToFrame(currentTime)}
        tabIndex={0}
        onPointerDown={beginSelection}
        onPointerMove={extendSelection}
        onPointerUp={finishSelection}
        onPointerCancel={cancelSelection}
      >
        <div className="bp-trim-rail" />
        {ranges.map((range, index) => (
          <button
            key={`${range.start}-${range.end}-${index}`}
            type="button"
            className={`bp-trim-band${selected === index ? " is-selected" : ""}`}
            style={{
              left: `${(range.start / frameCount) * 100}%`,
              width: `${((range.end - range.start) / frameCount) * 100}%`,
            }}
            title={`删除 ${formatSeconds(frameToTime(range.start))} – ${formatSeconds(frameToTime(range.end))}`}
            onPointerDown={(event) => {
              event.stopPropagation();
              setSelected(index);
              seek(frameToTime(range.start));
            }}
          />
        ))}
        {draft && draft.end > draft.start && (
          <div
            className="bp-trim-band is-draft"
            style={{
              left: `${(draft.start / frameCount) * 100}%`,
              width: `${((draft.end - draft.start) / frameCount) * 100}%`,
            }}
          />
        )}
        <div className="bp-trim-playhead" style={{ left: `${playhead}%` }} />
      </div>

      <div className="bp-trim-scale">
        <span>0</span>
        <span>拖拽时间轴选择要删除的片段</span>
        <span>{frameCount}</span>
      </div>

      {ranges.length > 0 && (
        <ul className="bp-trim-ranges">
          {ranges.map((range, index) => (
            <li key={`${range.start}-${range.end}-${index}`} className={selected === index ? "is-selected" : ""}>
              <button type="button" className="bp-trim-range-label" onClick={() => { setSelected(index); seek(frameToTime(range.start)); }}>
                #{index + 1} · 帧 {range.start}–{range.end}
                <span>
                  {formatSeconds(frameToTime(range.start))} – {formatSeconds(frameToTime(range.end))}
                </span>
              </button>
              {selected === index && (
                <span className="bp-trim-nudge">
                  <em>起</em>
                  <button type="button" onClick={() => nudgeSelected("start", -1)} title="起点向前一帧">−</button>
                  <button type="button" onClick={() => nudgeSelected("start", 1)} title="起点向后一帧">+</button>
                  <em>止</em>
                  <button type="button" onClick={() => nudgeSelected("end", -1)} title="终点向前一帧">−</button>
                  <button type="button" onClick={() => nudgeSelected("end", 1)} title="终点向后一帧">+</button>
                </span>
              )}
              <button type="button" className="bp-trim-remove" onClick={() => removeRange(index)} title="移除这段删除区间">×</button>
            </li>
          ))}
        </ul>
      )}

      {error && <p className="bp-trim-error" role="alert">{error}</p>}

      <div className="bp-trim-actions">
        <button
          type="button"
          className="secondary"
          disabled={busy || ranges.length === 0}
          onClick={() => { setRanges([]); setSelected(null); }}
        >
          清空所选片段
        </button>
        <button type="button" className="primary" disabled={busy || ranges.length === 0 || keptFrames < 1} onClick={submit}>
          {submitting ? "正在提交…" : "删除所选片段并更新原数据集"}
        </button>
      </div>

      {job && (
        <div className={`bp-trim-status is-${job.state.toLowerCase()}`}>
          <div>
            <span className="bp-trim-state">{statusText(job)}</span>
            {job.manual_summary && (
              <span className="bp-trim-sub">
                删除 {job.manual_summary.frames_removed ?? "?"} 帧 · 保留 {job.manual_summary.frames_kept ?? "?"} 帧
              </span>
            )}
          </div>
          {job.error && <p className="bp-trim-error">{job.error}</p>}
          {finished && (
            <button type="button" className="secondary" onClick={openResult}>
              {job.in_place ? "刷新当前预览" : "预览裁剪结果"}
            </button>
          )}
        </div>
      )}
    </section>
  );
}
