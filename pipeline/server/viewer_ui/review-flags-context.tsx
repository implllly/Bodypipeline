"use client";

import React, {
  createContext,
  useContext,
  useState,
  useCallback,
  useMemo,
  useEffect,
} from "react";
import { ReviewFlagStore, type CleanupState } from "@/utils/review-flags-store";

type Flags = {
  flagged: Set<number>;
  count: number;
  has: (id: number) => boolean;
  toggle: (id: number) => void;
  addMany: (ids: number[]) => void;
  clear: () => void;
  storageError: string;
  remap: (cleanup: CleanupState) => boolean;
  canReload: () => boolean;
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
  const [, render] = useState(0);
  const [ready, setReady] = useState(false);
  const store = useMemo(
    () =>
      new ReviewFlagStore(
        {
          getItem: (key) => localStorage.getItem(key),
          setItem: (key, value) => localStorage.setItem(key, value),
        },
        repoId,
        () => render((n) => n + 1),
      ),
    [repoId],
  );
  useEffect(() => {
    store.refresh();
    setReady(true);
    const update = (event: StorageEvent) => {
      // Read the latest record, not a queued event's potentially stale payload.
      if (event.key === store.key || event.key === null) store.refresh();
    };
    window.addEventListener("storage", update);
    return () => window.removeEventListener("storage", update);
  }, [store]);
  const toggle = useCallback(
    (id: number) =>
      store.change((previous) => {
        const next = new Set(previous);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      }),
    [store],
  );
  const addMany = useCallback(
    (ids: number[]) =>
      store.change((previous) => new Set([...previous, ...ids])),
    [store],
  );
  const clear = useCallback(() => store.change(() => new Set(), true), [store]);
  const remap = useCallback(
    (cleanup: CleanupState) => store.sync(cleanup),
    [store],
  );
  const canReload = useCallback(() => store.persisted, [store]);
  const flagged = store.flags;
  const value = {
    flagged,
    count: flagged.size,
    has: (id: number) => flagged.has(id),
    toggle,
    addMany,
    clear,
    storageError: store.error,
    remap,
    canReload,
  };
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
