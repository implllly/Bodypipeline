import { flagStorageKey, readFlags, remapFlags } from "./batch-preview-model";

export type CleanupState = {
  cleanup_generation: number;
  cleanup_history: {
    id: string;
    from: number;
    to: number;
    mapping: Record<string, number>;
  }[];
};
type Snapshot = {
  version: 2;
  generation: number | null;
  flags: number[];
  notice: string;
  // Retain ambiguous records for recovery, but never display them on new IDs.
  unmapped?: { generation: number | null; flags: number[] };
};
type StorageIO = Pick<Storage, "getItem" | "setItem">;
const unavailable =
  "浏览器存储不可用，待检查标记仅在本页保留；跨标签同步已暂停";

/** One atomic record binds flags to their numbering generation. IO is injectable. */
export class ReviewFlagStore {
  readonly key: string;
  private current: Snapshot = {
    version: 2,
    generation: null,
    flags: [],
    notice: "",
  };
  private dirty = false;
  private ioError = "";
  private observed: string | null | undefined;

  constructor(
    private storage: StorageIO,
    private repoId: string,
    private notify: () => void,
  ) {
    this.key = flagStorageKey(repoId).replace(":v1:", ":v2:");
  }

  get flags() {
    return new Set(this.current.flags);
  }
  get persisted() {
    return !this.dirty;
  }
  get error() {
    return [this.current.notice, this.ioError].filter(Boolean).join("；");
  }

  private read(): Snapshot {
    const raw = this.storage.getItem(this.key);
    this.observed = raw;
    if (raw) {
      const value = JSON.parse(raw);
      if (
        value?.version !== 2 ||
        !(
          value.generation === null ||
          (Number.isSafeInteger(value.generation) && value.generation >= 0)
        ) ||
        !Array.isArray(value.flags)
      )
        throw new Error("Invalid review flags");
      return {
        ...value,
        flags: [...readFlags(JSON.stringify(value.flags))],
        notice: typeof value.notice === "string" ? value.notice : "",
      };
    }
    return {
      version: 2,
      generation: null,
      flags: [...readFlags(this.storage.getItem(flagStorageKey(this.repoId)))],
      notice: "",
    };
  }

  refresh() {
    // A failed write must not be replaced by persisted (possibly stale) flags.
    if (this.dirty) return;
    try {
      const next = this.read();
      if (
        this.current.generation !== null &&
        next.generation !== null &&
        next.generation < this.current.generation
      )
        return;
      this.current = next;
      this.ioError = "";
    } catch {
      this.ioError = unavailable;
    }
    this.notify();
  }

  private persist(next: Snapshot) {
    const wasDirty = this.dirty;
    this.current = next;
    this.dirty = true;
    try {
      // While degraded, do not overwrite another tab's newer persisted work.
      const remote = this.storage.getItem(this.key);
      if (wasDirty && this.observed !== undefined && remote !== this.observed) {
        this.ioError =
          "其他标签页已修改标记；本页未保存记录仍保留，跨标签同步已暂停，请核对后刷新";
        this.notify();
        return;
      }
      const raw = JSON.stringify(next);
      this.storage.setItem(this.key, raw);
      this.observed = raw;
      this.dirty = false;
      this.ioError = "";
    } catch {
      this.ioError = unavailable;
    }
    this.notify();
  }

  change(
    transform: (previous: Set<number>) => Set<number>,
    clearNotice = false,
  ) {
    this.refresh();
    this.persist({
      ...this.current,
      flags: [...transform(this.flags)],
      ...(clearNotice ? { notice: "", unmapped: undefined } : {}),
    });
  }

  /** False means the response is older than the flags. IO failure stays in memory. */
  sync(cleanup: CleanupState): boolean {
    this.refresh();
    const target = cleanup.cleanup_generation;
    if (
      !Number.isSafeInteger(target) ||
      target < 0 ||
      !Array.isArray(cleanup.cleanup_history)
    )
      throw new Error("清理版本缺失，请更新 Viewer 服务后重试");
    const previous = this.current;
    if (previous.generation !== null && target < previous.generation)
      return false;
    let generation = previous.generation;
    let flags = this.flags;
    if (generation === null && (target === 0 || !flags.size))
      generation = target;
    const history = [...cleanup.cleanup_history].sort(
      (a, b) => a.from - b.from,
    );
    if (generation !== null) {
      for (const item of history) {
        if (
          item.from === generation &&
          item.to === generation + 1 &&
          item.to <= target
        ) {
          flags = remapFlags(flags, item.mapping);
          generation = item.to;
        }
      }
    }
    if (generation !== target) {
      this.persist({
        ...previous,
        generation: target,
        flags: [],
        unmapped: previous.unmapped || {
          generation: previous.generation,
          flags: previous.flags,
        },
        notice:
          "数据已重编号，但待检查标记的映射历史不完整；原记录已保留，请重新核查并标记",
      });
    } else {
      this.persist({ ...previous, generation: target, flags: [...flags] });
    }
    return true;
  }
}
