"use client";

import React, { useEffect, useRef, useState } from "react";
import {
  segmentMenuAnchor,
  type SegmentMenuAnchor,
} from "./task-annotation-menu";
import {
  boundaryTime,
  gestureRange,
  timelineBoundary,
  timelineGesture,
  timelineTime,
  type RangeGesture,
  type TimelineGesture,
} from "../utils/task-annotation-model";

export interface TimelineSegment {
  id: string;
  start: number;
  end: number;
  label: string;
  description: string;
}
export interface TimelineTrack {
  id: string;
  name: string;
  background?: boolean;
  segments: TimelineSegment[];
}
interface Props {
  tracks: TimelineTrack[];
  timestamps: number[];
  duration: number;
  currentTime: number;
  activeTrack: string;
  selectedId: string | null;
  onNew: (track: string) => void;
  onSelect: (track: string, id: string, time?: number) => void;
  onCommit: (
    track: string,
    id: string | null,
    range: { start: number; end: number },
  ) => void;
  onSeek: (time: number) => void;
  onPause: () => void;
  onMenu: (track: string, id: string, anchor: SegmentMenuAnchor) => void;
}
interface Drag {
  pointer: number;
  mode: TimelineGesture;
  anchor: number;
  originX: number;
  initialTime: number;
  left: number;
  width: number;
  original?: TimelineSegment;
  range: { start: number; end: number };
  moved: boolean;
}

function Track({
  track,
  index,
  ...props
}: Omit<Props, "tracks"> & { track: TimelineTrack; index: number }) {
  const rail = useRef<HTMLDivElement>(null);
  const drag = useRef<Drag | null>(null);
  const [preview, setPreview] = useState<Drag | null>(null);
  const count = props.timestamps.length;
  const release = (g: Drag) => {
    if (rail.current?.hasPointerCapture(g.pointer))
      rail.current.releasePointerCapture(g.pointer);
  };
  const cancel = () => {
    const g = drag.current;
    if (!g) return;
    drag.current = null;
    setPreview(null);
    release(g);
    props.onSeek(g.initialTime);
  };
  useEffect(() => {
    const escape = (e: KeyboardEvent) => {
      if (e.key === "Escape" && drag.current) {
        e.preventDefault();
        cancel();
      }
    };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  });
  useEffect(
    () => () => {
      drag.current = null;
    },
    [],
  );
  const begin = (
    e: React.PointerEvent<HTMLButtonElement>,
    requested: RangeGesture,
    segment?: TimelineSegment,
  ) => {
    const mode = timelineGesture(requested, e.shiftKey);
    if (
      e.button !== 0 ||
      !e.isPrimary ||
      !rail.current ||
      props.duration <= 0 ||
      (mode !== "scrub" && !count)
    )
      return;
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.focus();
    props.onPause();
    const bounds = rail.current.getBoundingClientRect();
    const anchor = timelineBoundary(
      props.timestamps,
      props.duration,
      e.clientX,
      bounds.left,
      bounds.width,
    );
    drag.current = {
      pointer: e.pointerId,
      mode,
      anchor,
      originX: e.clientX,
      initialTime: props.currentTime,
      left: bounds.left,
      width: bounds.width,
      original: segment,
      range: segment
        ? { start: segment.start, end: segment.end }
        : gestureRange("create", anchor, anchor, count),
      moved: false,
    };
    rail.current.setPointerCapture(e.pointerId);
    if (mode === "scrub")
      props.onSeek(
        timelineTime(props.duration, e.clientX, bounds.left, bounds.width),
      );
  };
  const calculate = (g: Drag, x: number) => {
    const current = timelineBoundary(
      props.timestamps,
      props.duration,
      x,
      g.left,
      g.width,
    );
    return {
      ...g,
      moved: g.moved || Math.abs(x - g.originX) >= 4,
      range:
        g.mode === "scrub"
          ? g.range
          : gestureRange(g.mode, g.anchor, current, count, g.original),
    };
  };
  const move = (e: React.PointerEvent<HTMLDivElement>) => {
    const g = drag.current;
    if (!g || g.pointer !== e.pointerId) return;
    const next = calculate(g, e.clientX);
    drag.current = next;
    if (next.moved && next.mode !== "scrub") setPreview(next);
    props.onSeek(
      timelineTime(props.duration, e.clientX, next.left, next.width),
    );
  };
  const finish = (e: React.PointerEvent<HTMLDivElement>) => {
    const g = drag.current;
    if (!g || g.pointer !== e.pointerId) return;
    const next = calculate(g, e.clientX);
    drag.current = null;
    setPreview(null);
    release(g);
    const time = timelineTime(props.duration, e.clientX, next.left, next.width);
    if (next.moved && next.mode !== "scrub")
      props.onCommit(track.id, next.original?.id || null, next.range);
    else if (!next.moved && g.original)
      props.onSelect(track.id, g.original.id, time);
    props.onSeek(time);
  };
  const position = (range: {
    start: number;
    end: number;
  }): React.CSSProperties =>
    ({
      left: `${props.duration ? (boundaryTime(props.timestamps, range.start, props.duration) / props.duration) * 100 : 0}%`,
      width: `${props.duration ? ((boundaryTime(props.timestamps, range.end, props.duration) - boundaryTime(props.timestamps, range.start, props.duration)) / props.duration) * 100 : 0}%`,
      "--ta-track": track.background
        ? "var(--text-faint, #8793a5)"
        : `var(--ta-track-${index % 4})`,
    }) as React.CSSProperties;
  const keyboardResize = (
    e: React.KeyboardEvent<HTMLButtonElement>,
    mode: "start" | "end",
    segment: TimelineSegment,
  ) => {
    if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
    e.preventDefault();
    const delta = (e.key === "ArrowRight" ? 1 : -1) * (e.shiftKey ? 10 : 1);
    const boundary = mode === "start" ? segment.start : segment.end;
    props.onCommit(
      track.id,
      segment.id,
      gestureRange(mode, boundary, boundary + delta, count, segment),
    );
  };
  return (
    <div className={`ta-track ${track.background ? "ta-null-track" : ""}`}>
      <button
        className={`ta-track-label ${props.activeTrack === track.id ? "active" : ""}`}
        onClick={() => props.onNew(track.id)}
      >
        <span className="ta-index">
          {track.background ? "∅" : String(index + 1).padStart(2, "0")}
        </span>
        <span>{track.name}</span>
        <small>
          {track.segments.length} {track.background ? "段" : "次"}
        </small>
      </button>
      <div
        ref={rail}
        className={`ta-rail ${preview ? "ta-dragging" : ""}`}
        onPointerMove={move}
        onPointerUp={finish}
        onPointerCancel={cancel}
        onLostPointerCapture={cancel}
      >
        <button
          className="ta-seek-area"
          aria-label={`在${track.name}轨道拖动预览，Shift 拖动添加片段`}
          disabled={props.duration <= 0}
          onPointerDown={(e) => begin(e, "create")}
          onClick={(e) => {
            if (e.detail === 0) props.onNew(track.id);
          }}
        />
        {track.segments.map((segment) => {
          const shown =
            preview?.original?.id === segment.id ? preview.range : segment;
          const selected = props.selectedId === segment.id;
          return (
            <div
              className={`ta-segment-box ${selected ? "selected" : ""} ${track.background ? "ta-null-segment" : ""}`}
              style={position(shown)}
              key={segment.id}
              onContextMenu={(e) => {
                e.preventDefault();
                e.stopPropagation();
                cancel();
                props.onMenu(
                  track.id,
                  segment.id,
                  segmentMenuAnchor(
                    e.currentTarget.querySelector<HTMLButtonElement>(
                      ".ta-segment",
                    ) || e.currentTarget,
                    e.clientX,
                    e.clientY,
                  ),
                );
              }}
              onKeyDown={(e) => {
                if (
                  e.key === "ContextMenu" ||
                  (e.shiftKey && e.key === "F10")
                ) {
                  e.preventDefault();
                  e.stopPropagation();
                  cancel();
                  props.onMenu(
                    track.id,
                    segment.id,
                    segmentMenuAnchor(e.target as HTMLElement),
                  );
                }
              }}
            >
              <button
                className="ta-segment"
                aria-label={segment.description}
                title={`${segment.description} · 帧 ${shown.start}–${shown.end}（结束不含）；普通拖动预览，Shift＋拖动移动片段`}
                onPointerDown={(e) => begin(e, "move", segment)}
                onClick={(e) => {
                  if (e.detail === 0) props.onSelect(track.id, segment.id);
                }}
              >
                {segment.label}
              </button>
              <button
                className="ta-resize ta-resize-start"
                aria-label={`调整${segment.description}起点`}
                title="Shift＋拖动调整起点；方向键微调，Shift 加速"
                onPointerDown={(e) => begin(e, "start", segment)}
                onKeyDown={(e) => keyboardResize(e, "start", segment)}
                onClick={(e) => {
                  if (e.detail === 0) props.onSelect(track.id, segment.id);
                }}
              />
              <button
                className="ta-resize ta-resize-end"
                aria-label={`调整${segment.description}终点`}
                title="Shift＋拖动调整终点；方向键微调，Shift 加速"
                onPointerDown={(e) => begin(e, "end", segment)}
                onKeyDown={(e) => keyboardResize(e, "end", segment)}
                onClick={(e) => {
                  if (e.detail === 0) props.onSelect(track.id, segment.id);
                }}
              />
            </div>
          );
        })}
        {preview && (
          <div className="ta-range-preview" style={position(preview.range)}>
            <span>
              帧 {preview.range.start}–{preview.range.end}
            </span>
          </div>
        )}
        <span
          className="ta-playhead"
          style={{
            left: `${props.duration ? Math.min((props.currentTime / props.duration) * 100, 100) : 0}%`,
          }}
        />
      </div>
    </div>
  );
}

export function TaskAnnotationTimeline(props: Props) {
  const [editing, setEditing] = useState(false);
  useEffect(() => {
    const modifier = (e: KeyboardEvent) => setEditing(e.shiftKey);
    const clear = () => setEditing(false);
    window.addEventListener("keydown", modifier);
    window.addEventListener("keyup", modifier);
    window.addEventListener("blur", clear);
    return () => {
      window.removeEventListener("keydown", modifier);
      window.removeEventListener("keyup", modifier);
      window.removeEventListener("blur", clear);
    };
  }, []);
  return (
    <div
      className={`ta-timeline-tracks ${editing ? "ta-shift-editing" : ""}`}
      onPointerMove={(e) => {
        if (editing !== e.shiftKey) setEditing(e.shiftKey);
      }}
    >
      {props.tracks.map((track, index) => (
        <Track {...props} key={track.id} track={track} index={index} />
      ))}
    </div>
  );
}
