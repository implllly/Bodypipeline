import { expect, test } from "bun:test";
import {
  ReviewFlagStore,
  type CleanupState,
} from "../pipeline/server/viewer_ui/review-flags-store";

const a = { id: "A", from: 0, to: 1, mapping: { 1: 0, 2: 1, 3: 2, 4: 3 } };
const b = { id: "B", from: 1, to: 2, mapping: { 1: 0, 2: 1, 3: 2 } };
const original: CleanupState = { cleanup_generation: 0, cleanup_history: [] };
const first: CleanupState = { cleanup_generation: 1, cleanup_history: [a] };
const second: CleanupState = { cleanup_generation: 2, cleanup_history: [a, b] };
function fixture() {
  const records = new Map<string, string>();
  let failRead = false,
    failWrite = false;
  const io = {
    getItem(key: string) {
      if (failRead) throw new Error("blocked");
      return records.get(key) || null;
    },
    setItem(key: string, value: string) {
      if (failWrite) throw new Error("quota");
      records.set(key, value);
    },
  };
  return {
    records,
    io,
    store: new ReviewFlagStore(io, "bp/test", () => {}),
    failRead: (value: boolean) => {
      failRead = value;
    },
    failWrite: (value: boolean) => {
      failWrite = value;
    },
  };
}
const mark = (store: ReviewFlagStore, id: number) =>
  store.change((previous) => new Set([...previous, id]));

test("closed browser catches up on multiple cleanups in order, exactly once", () => {
  const { store, io } = fixture();
  store.sync(original);
  mark(store, 3);
  const reopened = new ReviewFlagStore(io, "bp/test", () => {});
  expect(reopened.sync(second)).toBe(true);
  expect([...reopened.flags]).toEqual([1]);
  reopened.sync(second);
  expect([...reopened.flags]).toEqual([1]);
});

test("stale responses and other tabs cannot remap a newer generation backwards", () => {
  const { store, io } = fixture();
  store.sync(original);
  mark(store, 4);
  store.sync(first);
  const other = new ReviewFlagStore(io, "bp/test", () => {});
  other.refresh();
  store.sync(second);
  expect(other.sync(first)).toBe(false);
  expect([...other.flags]).toEqual([2]);
  expect(store.sync(original)).toBe(false);
  expect([...store.flags]).toEqual([2]);
});

test("missing history quarantines old flags instead of marking unrelated videos", () => {
  const { store, records } = fixture();
  store.sync(original);
  mark(store, 3);
  store.sync({ ...second, cleanup_history: [b] });
  expect([...store.flags]).toEqual([]);
  expect(store.error).toContain("映射历史不完整");
  expect(JSON.parse(records.get(store.key)!).unmapped.flags).toEqual([3]);
  mark(store, 1);
  store.sync(second);
  expect([...store.flags]).toEqual([1]);
  expect(store.error).toContain("映射历史不完整");
});

test("legacy flags migrate only when their numbering is unambiguous", () => {
  const { store, records } = fixture();
  records.set("bodypipeline:review-flags:v1:bp%2Ftest", "[3]");
  store.sync(original);
  expect([...store.flags]).toEqual([3]);
  const ambiguous = fixture();
  ambiguous.records.set("bodypipeline:review-flags:v1:bp%2Ftest", "[3]");
  ambiguous.store.sync(second);
  expect([...ambiguous.store.flags]).toEqual([]);
  expect(ambiguous.store.error).toContain("原记录已保留");
  expect(ambiguous.records.get("bodypipeline:review-flags:v1:bp%2Ftest")).toBe(
    "[3]",
  );
});

test("write recovery keeps all in-page marks and removals, not the old disk copy", () => {
  const f = fixture();
  f.store.sync(original);
  mark(f.store, 4);
  f.failWrite(true);
  mark(f.store, 0);
  mark(f.store, 1);
  f.store.change((previous) => new Set([...previous].filter((id) => id !== 4)));
  expect(f.store.error).toContain("本页保留");
  f.store.refresh();
  f.failWrite(false);
  mark(f.store, 2);
  expect([...f.store.flags]).toEqual([0, 1, 2]);
  expect(JSON.parse(f.records.get(f.store.key)!).flags).toEqual([0, 1, 2]);
  expect(f.store.error).toBe("");
});

test("failed cleanup writes persist neither a marker nor partially remapped flags", () => {
  const f = fixture();
  f.store.sync(original);
  mark(f.store, 3);
  const persisted = f.records.get(f.store.key);
  f.failWrite(true);
  expect(f.store.sync(first)).toBe(true);
  expect(f.store.persisted).toBe(false);
  expect([...f.store.flags]).toEqual([2]);
  expect(f.records.get(f.store.key)).toBe(persisted);
  expect(f.store.sync(first)).toBe(true);
  expect(f.store.persisted).toBe(false);
  expect([...f.store.flags]).toEqual([2]);
  f.failWrite(false);
  expect(f.store.sync(first)).toBe(true);
  expect(JSON.parse(f.records.get(f.store.key)!).generation).toBe(1);
  expect(f.store.persisted).toBe(true);
  expect([...f.store.flags]).toEqual([2]);
});

test("queued storage events read current state and normal tabs preserve sequential edits", () => {
  const { store, io } = fixture();
  store.sync(original);
  const other = new ReviewFlagStore(io, "bp/test", () => {});
  mark(store, 1);
  mark(other, 2);
  store.refresh();
  expect([...store.flags]).toEqual([1, 2]);
  const different = new ReviewFlagStore(io, "bp/other", () => {});
  different.refresh();
  expect([...different.flags]).toEqual([]);
});

test("read failure recovers without discarding in-page marks", () => {
  const f = fixture();
  f.failRead(true);
  f.failWrite(true);
  f.store.refresh();
  mark(f.store, 0);
  f.failRead(false);
  f.failWrite(false);
  mark(f.store, 1);
  expect([...f.store.flags]).toEqual([0, 1]);
  expect(f.store.sync(original)).toBe(true);
});

test("degraded tab never overwrites another tab's newer flags on recovery", () => {
  const f = fixture();
  f.store.sync(original);
  f.failWrite(true);
  mark(f.store, 3);
  f.failWrite(false);
  const other = new ReviewFlagStore(f.io, "bp/test", () => {});
  other.sync(second);
  mark(other, 1);
  const remote = f.records.get(f.store.key);
  mark(f.store, 4);
  expect(f.records.get(f.store.key)).toBe(remote);
  expect([...f.store.flags]).toEqual([3, 4]);
  expect(f.store.error).toContain("其他标签页");
});
