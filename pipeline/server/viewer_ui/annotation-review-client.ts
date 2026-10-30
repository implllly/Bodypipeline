import type { LanguageAtom } from "../types/language.types";
import type { TaskDocument } from "./task-annotation-model";

export interface Review {
  fps: number;
  frames: number;
  revision: number;
  source_signature: string;
  segments: { start_frame: number; end_frame: number; text: string }[];
  atoms?: LanguageAtom[];
  task_document?: TaskDocument;
  has_draft?: boolean;
}
export function reviewUrl(repo: string, episode?: number) {
  const base = `/api/subtask-review/${encodeURIComponent(repo.slice(3))}`;
  return episode == null ? base : `${base}/episodes/${episode}`;
}
export async function reviewRequest<T>(
  url: string,
  body?: unknown,
): Promise<T> {
  const r = await fetch(url, {
    credentials: "same-origin",
    cache: "no-store",
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok)
    throw new Error(
      typeof data.detail === "string" ? data.detail : `请求失败 (${r.status})`,
    );
  return data as T;
}
export function reviewAtoms(review: Review): LanguageAtom[] {
  return [
    ...(review.atoms || []),
    ...review.segments.map((s) => ({
      role: "assistant" as const,
      style: "subtask" as const,
      content: s.text,
      timestamp: s.start_frame / review.fps,
      end_timestamp: s.end_frame / review.fps,
      camera: null,
      tool_calls: null,
    })),
  ];
}
export function insertReviewAtoms(
  previous: LanguageAtom[],
  added: LanguageAtom[],
  review: Review,
): LanguageAtom[] {
  let result = [...previous];
  for (const raw of added) {
    if (raw.style !== "subtask") {
      result.push(raw);
      continue;
    }
    const timestamp =
      Math.max(
        0,
        Math.min(review.frames - 1, Math.round(raw.timestamp * review.fps)),
      ) / review.fps;
    const containing = result.find(
      (a) =>
        a.style === "subtask" &&
        a.timestamp < timestamp &&
        (a.end_timestamp ?? 0) > timestamp,
    );
    const next = result
      .filter((a) => a.style === "subtask" && a.timestamp > timestamp)
      .sort((a, b) => a.timestamp - b.timestamp)[0];
    const end =
      raw.end_timestamp ??
      containing?.end_timestamp ??
      next?.timestamp ??
      review.frames / review.fps;
    const atom = {
      ...raw,
      timestamp,
      end_timestamp: Math.max(
        timestamp + 1 / review.fps,
        Math.min(review.frames / review.fps, end),
      ),
    };
    // Preserve boundary insertion, but only split an actually covered interval.
    if (containing)
      result = result.map((a) =>
        a === containing ? { ...a, end_timestamp: timestamp } : a,
      );
    result.push(atom);
  }
  return result;
}
export function reviewPayload(review: Review, atoms: LanguageAtom[]) {
  const segments = atoms
    .filter((a) => a.style === "subtask")
    .map((a) => ({
      start_frame: Math.round(a.timestamp * review.fps),
      end_frame: Math.round(
        (a.end_timestamp ?? a.timestamp + 1 / review.fps) * review.fps,
      ),
      text: a.content || "",
    }));
  const sorted = [...segments].sort((a, b) => a.start_frame - b.start_frame);
  let end = 0;
  for (const s of sorted) {
    if (
      !Number.isFinite(s.start_frame) ||
      !Number.isFinite(s.end_frame) ||
      !s.text.trim() ||
      s.text.length > 400 ||
      s.start_frame < end ||
      s.start_frame < 0 ||
      s.end_frame <= s.start_frame ||
      s.end_frame > review.frames
    )
      throw new Error(
        "Subtask 区间不能重叠，须在 Episode 内且文本为 1–400 字符；请调整结束边界。",
      );
    end = s.end_frame;
  }
  return {
    revision: review.revision,
    source_signature: review.source_signature,
    segments: sorted,
    atoms: atoms
      .filter((a) => a.style !== "subtask")
      .map(({ end_timestamp: _end, ...a }) => a),
  };
}
