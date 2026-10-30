"use client";

import React, {
  createContext,
  useContext,
  useState,
  useCallback,
  useMemo,
  useEffect,
} from "react";
import {
  flagStorageKey,
  readFlags,
  remapFlags,
} from "@/utils/batch-preview-model";

type Flags = {
  flagged: Set<number>;
  count: number;
  has: (id: number) => boolean;
  toggle: (id: number) => void;
  addMany: (ids: number[]) => void;
  clear: () => void;
  storageError: string;
  remap: (mapping: Record<string, number>, operationId: string) => void;
};
const Context = createContext<Flags | undefined>(undefined);
export function useFlaggedEpisodes() {
  const value = useContext(Context);
  if (!value) throw new Error("Missing FlaggedEpisodesProvider");
  return value;
}

function DatasetFlags({
  repoId,
  children,
}: {
  repoId: string;
  children: React.ReactNode;
}) {
  const key = flagStorageKey(repoId);
  const [flagged, setFlags] = useState<Set<number>>(new Set());
  const [storageError, setStorageError] = useState("");
  const [ready, setReady] = useState(false);
  useEffect(() => {
    try {
      setFlags(readFlags(localStorage.getItem(key)));
    } catch {
      setStorageError("浏览器存储不可用，待检查标记仅在本页保留");
    }
    setReady(true);
    const update = (event: StorageEvent) => {
      if (event.key === key) setFlags(readFlags(event.newValue));
    };
    window.addEventListener("storage", update);
    return () => window.removeEventListener("storage", update);
  }, [key]);
  const change = useCallback(
    (transform: (previous: Set<number>) => Set<number>) => {
      // Read latest persisted flags so another open tab's marks are preserved.
      let next: Set<number>;
      try {
        next = transform(readFlags(localStorage.getItem(key)));
        localStorage.setItem(key, JSON.stringify([...next]));
        setFlags(next);
        setStorageError("");
      } catch {
        setFlags((previous) => transform(previous));
        setStorageError("浏览器存储不可用，待检查标记仅在本页保留");
      }
    },
    [key],
  );
  const toggle = useCallback(
    (id: number) =>
      change((previous) => {
        const next = new Set(previous);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      }),
    [change],
  );
  const addMany = useCallback(
    (ids: number[]) => change((previous) => new Set([...previous, ...ids])),
    [change],
  );
  const clear = useCallback(() => change(() => new Set()), [change]);
  const remap = useCallback(
    (mapping: Record<string, number>, operationId: string) => {
      const marker = `${key}:cleanup`;
      try {
        if (localStorage.getItem(marker) === operationId) return;
        change((previous) => remapFlags(previous, mapping));
        localStorage.setItem(marker, operationId);
      } catch {
        setStorageError("清理后的待检查编号未能保存，请刷新检查");
      }
    },
    [key, change],
  );
  const value = useMemo(
    () => ({
      flagged,
      count: flagged.size,
      has: (id: number) => flagged.has(id),
      toggle,
      addMany,
      clear,
      storageError,
      remap,
    }),
    [flagged, toggle, addMany, clear, storageError, remap],
  );
  return (
    <Context.Provider value={value}>{ready ? children : null}</Context.Provider>
  );
}

export function FlaggedEpisodesProvider({
  repoId,
  children,
}: {
  repoId: string;
  children: React.ReactNode;
}) {
  return (
    <DatasetFlags key={repoId} repoId={repoId}>
      {children}
    </DatasetFlags>
  );
}
