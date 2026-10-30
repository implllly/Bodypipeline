import { expect, test } from "bun:test";
import {
  batchCatalog,
  batchPage,
  clipBounds,
  clipPosition,
  flagStorageKey,
  readFlags,
  remapFlags,
} from "../pipeline/server/viewer_ui/batch-preview-model";

const info = {
  total_episodes: 2,
  fps: 20,
  chunks_size: 1000,
  video_path:
    "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
  features: {
    "observation.images.main": { dtype: "video" },
    action: { dtype: "float32" },
  },
};
const url = (path: string) => `/dataset/${path}`;

test("v2 previews use metadata, padded chunk paths and each episode's own duration", () => {
  const catalog = batchCatalog(
    "bp/a",
    "v2.1",
    info,
    [
      { episode_index: 999, length: 120 },
      { episode_index: 1000, length: 240 },
    ],
    url,
  );
  const clips = catalog.clips["observation.images.main"];
  expect(catalog.episodes).toEqual([999, 1000]);
  expect(catalog.cameras).toEqual(["observation.images.main"]);
  expect(clips[999].url).toBe(
    "/dataset/videos/chunk-000/observation.images.main/episode_000999.mp4",
  );
  expect(clips[1000].url).toContain("chunk-001/");
  expect(clips[999].end).toBe(6);
  expect(clips[1000].end).toBe(12);
});

test("v3 shared-file playback seeks and stops within each camera's episode offsets", () => {
  const catalog = batchCatalog(
    "bp/b",
    "v3.0",
    {
      ...info,
      total_episodes: 1,
      video_path:
        "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
    },
    [
      {
        episode_index: 4,
        "videos/observation.images.main/chunk_index": 2n,
        "videos/observation.images.main/file_index": 11n,
        "videos/observation.images.main/from_timestamp": 20,
        "videos/observation.images.main/to_timestamp": 26,
      },
    ],
    url,
  );
  const clip = catalog.clips["observation.images.main"][4];
  expect(clip.url).toBe(
    "/dataset/videos/observation.images.main/chunk-002/file-011.mp4",
  );
  expect(clipBounds(clip, 100)).toEqual([20, 25.95]);
  expect(clipPosition(clip, 100, 0)).toBe(20);
  expect(clipPosition(clip, 100, 0.5)).toBeCloseTo(22.975);
  expect(clipPosition(clip, 100, 3)).toBe(25.95);
  expect(clipPosition(clip, 100, -2)).toBe(20);
  expect(clipBounds(clip, 24)).toEqual([20, 23.95]);
});

test("missing camera offsets never fall back to episode zero or another shard", () => {
  const catalog = batchCatalog(
    "bp/b",
    "v3.0",
    { ...info, total_episodes: 1 },
    [{ episode_index: 8, length: 80 }],
    url,
  );
  expect(catalog.episodes).toEqual([8]);
  expect(catalog.clips["observation.images.main"][8]).toBeUndefined();
  expect(() =>
    batchCatalog("bp/b", "v3.0", info, [{ episode_index: 0 }], url),
  ).toThrow("不完整");
  expect(() =>
    batchCatalog(
      "bp/b",
      "v2.1",
      info,
      [{ episode_index: 0 }, { episode_index: 0 }],
      url,
    ),
  ).toThrow("重复");
});

test("all four layouts paginate the complete batch, including episodes beyond 3000", () => {
  const episodes = Array.from({ length: 3100 }, (_, i) => i);
  for (const size of [4, 9, 16, 32]) {
    const all = Array.from(
      { length: Math.ceil(episodes.length / size) },
      (_, page) => batchPage(episodes, page, size),
    ).flat();
    expect(all).toEqual(episodes);
  }
  expect(batchPage([1, 2, 3, 4, 5], 3, 4)).toEqual([5]);
  expect(batchPage([], 0, 32)).toEqual([]);
});

test("review flags isolate datasets, reject malformed storage and remap deleted episodes", () => {
  expect(flagStorageKey("bp/a")).not.toBe(flagStorageKey("bp/b"));
  expect([...readFlags('[3,3,-1,2.5,"8",null,9]')]).toEqual([3, 9]);
  expect([...readFlags("bad json")]).toEqual([]);
  expect([...readFlags('{"bad":1}')]).toEqual([]);
  expect([...remapFlags(new Set([3, 9, 12]), { 3: 0, 12: 8 })]).toEqual([0, 8]);
});
