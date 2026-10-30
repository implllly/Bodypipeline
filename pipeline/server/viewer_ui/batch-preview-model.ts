/** Metadata-only batch playback. Never fetch frame/action parquet for the wall. */
export const GRID_SIZES = [4, 9, 16, 32] as const;
export type GridSize = (typeof GRID_SIZES)[number];
export type BatchClip = {
  episode: number;
  url: string;
  start: number;
  end: number | null;
  fps: number;
};
export type BatchCatalog = {
  repoId: string;
  cameras: string[];
  episodes: number[];
  clips: Record<string, Record<number, BatchClip>>;
  revision: string;
};
export type BatchInfo = {
  total_episodes: number;
  fps: number;
  chunks_size?: number;
  video_path: string | null;
  features: Record<string, { dtype: string }>;
  bodypipeline_revision?: string;
};

function numeric(value: unknown): number {
  return value === undefined || value === null || value === ""
    ? NaN
    : Number(value);
}
function index(value: unknown): number {
  const n = numeric(value);
  if (!Number.isSafeInteger(n) || n < 0)
    throw new Error("Episode 视频元数据编号无效");
  return n;
}
function videoPath(
  template: string,
  values: Record<string, string | number>,
): string {
  return template.replace(/\{(\w+)(?::0?(\d+)d)?\}/g, (_, key, width) => {
    if (!(key in values)) throw new Error(`视频路径缺少字段 ${key}`);
    return String(values[key]).padStart(Number(width) || 0, "0");
  });
}

export function batchCatalog(
  repoId: string,
  version: string,
  info: BatchInfo,
  rows: Record<string, unknown>[],
  urlFor: (path: string) => string,
): BatchCatalog {
  if (!info.video_path) throw new Error("这个数据集没有可供批量播放的视频");
  if (!Number.isFinite(info.fps) || info.fps <= 0)
    throw new Error("数据集帧率无效");
  const cameras = Object.keys(info.features).filter(
    (key) => info.features[key].dtype === "video",
  );
  if (!cameras.length) throw new Error("这个数据集没有视频视角");
  const clips: BatchCatalog["clips"] = Object.fromEntries(
    cameras.map((key) => [key, {}]),
  );
  const episodes: number[] = [];
  const seen = new Set<number>();
  for (const row of rows) {
    const episode = index(row.episode_index);
    if (seen.has(episode)) throw new Error(`Episode ${episode} 的元数据重复`);
    seen.add(episode);
    episodes.push(episode);
    for (const camera of cameras) {
      let start = 0;
      let end: number | null = null;
      const values: Record<string, string | number> = {
        video_key: camera,
        episode_index: episode,
        episode_chunk: Math.floor(episode / (info.chunks_size || 1000)),
      };
      if (version === "v3.0") {
        const field = (key: string) =>
          row[`videos/${camera}/${key}`] ?? row[`video_${key}`];
        // A missing view must not silently play another camera's shard.
        if (field("chunk_index") == null || field("file_index") == null)
          continue;
        values.chunk_index = index(field("chunk_index"));
        values.file_index = index(field("file_index"));
        start = numeric(field("from_timestamp"));
        end = numeric(field("to_timestamp"));
        if (
          !Number.isFinite(start) ||
          !Number.isFinite(end) ||
          start < 0 ||
          end <= start
        )
          continue;
      } else {
        const frames = numeric(row.length);
        if (Number.isFinite(frames) && frames > 0) end = frames / info.fps;
      }
      clips[camera][episode] = {
        episode,
        start,
        end,
        fps: info.fps,
        url: urlFor(videoPath(info.video_path, values)),
      };
    }
  }
  if (seen.size !== info.total_episodes)
    throw new Error(
      `Episode 元数据不完整：读取到 ${seen.size} / ${info.total_episodes} 条，请刷新或检查数据集`,
    );
  return {
    repoId,
    cameras,
    episodes: episodes.sort((a, b) => a - b),
    clips,
    revision: String(info.bodypipeline_revision || "original"),
  };
}

/** Last decodable frame, bounded by the file, including v3 shared-file offsets. */
export function clipBounds(
  clip: BatchClip,
  fileDuration: number,
): [number, number] {
  const end = Math.min(clip.end ?? fileDuration, fileDuration);
  return [clip.start, Math.max(clip.start, end - 1 / clip.fps)];
}
export function clipPosition(
  clip: BatchClip,
  duration: number,
  progress: number,
): number {
  const [start, end] = clipBounds(clip, duration);
  return start + (end - start) * Math.max(0, Math.min(1, progress));
}
export function batchPage(
  episodes: number[],
  page: number,
  size: number,
): number[] {
  const last = Math.max(0, Math.ceil(episodes.length / size) - 1);
  const safe = Math.max(0, Math.min(last, page));
  return episodes.slice(safe * size, (safe + 1) * size);
}
export function flagStorageKey(repoId: string): string {
  return `bodypipeline:review-flags:v1:${encodeURIComponent(repoId)}`;
}
export function readFlags(raw: string | null): Set<number> {
  try {
    const values: unknown = JSON.parse(raw || "[]");
    return new Set(
      Array.isArray(values)
        ? values.filter((n) => Number.isSafeInteger(n) && n >= 0)
        : [],
    );
  } catch {
    return new Set();
  }
}
export function remapFlags(
  flags: Set<number>,
  mapping: Record<string, number>,
): Set<number> {
  return new Set(
    [...flags]
      .filter((id) => Number.isSafeInteger(mapping[id]) && mapping[id] >= 0)
      .map((id) => mapping[id]),
  );
}
