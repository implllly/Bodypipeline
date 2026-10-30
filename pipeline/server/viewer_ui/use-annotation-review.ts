"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import type { LanguageAtom } from "../types/language.types";
import type { TaskDocument } from "./task-annotation-model";
import {
  reviewRequest,
  reviewUrl,
  type Review,
} from "./annotation-review-client";
import {
  readTaskReview,
  reviewSnapshot,
  taskReviewPayload,
} from "./task-review-client";

type LoadedReview = Review & { repo: string; episode: number };
type Pending = {
  atoms: LanguageAtom[];
  task_document: TaskDocument;
  saved: string;
  review: Review;
};
const backupKey = (repo: string, episode: number) =>
  `bp-task-review-unsaved:v1:${repo}:${episode}`;

/** One draft for the task studio and manual/VQA panel, one revision and save. */
export function useAnnotationReview(
  atoms: LanguageAtom[],
  setAtoms: Dispatch<SetStateAction<LanguageAtom[]>>,
  clearSelection: Dispatch<SetStateAction<number | null>>,
) {
  const [review, setReview] = useState<LoadedReview | null>(null);
  const [taskDocument, setTaskDocument] = useState<TaskDocument | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const dirty = !!review && reviewSnapshot(atoms, taskDocument) !== saved;
  const generation = useRef(0);
  const pendingSaves = useRef(new Map<string, Promise<void>>());
  const identity = useRef({ repo: "", episode: -1 });
  const cache = useRef(new Map<string, Pending>());
  const current = useRef({ atoms, taskDocument, saved, review });
  current.current = { atoms, taskDocument, saved, review };
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  useEffect(() => {
    if (!review || !taskDocument) return;
    try {
      const key = backupKey(review.repo, review.episode);
      if (dirty)
        sessionStorage.setItem(
          key,
          JSON.stringify({
            atoms,
            task_document: taskDocument,
            revision: review.revision,
            source_signature: review.source_signature,
          }),
        );
      else {
        const raw = sessionStorage.getItem(key);
        const backup = raw ? JSON.parse(raw) : null;
        // Preserve declined or stale backups; only remove a redundant copy.
        if (
          backup &&
          reviewSnapshot(backup.atoms, backup.task_document) ===
            reviewSnapshot(atoms, taskDocument)
        )
          sessionStorage.removeItem(key);
      }
    } catch {
      /* The in-memory draft remains editable; the Server is authoritative. */
    }
  }, [atoms, taskDocument, review, dirty]);

  const load = useCallback(
    (repo: string, episode: number) => {
      const previous = identity.current;
      const head = current.current;
      if (
        head.taskDocument &&
        head.review?.repo === previous.repo &&
        head.review?.episode === previous.episode &&
        reviewSnapshot(head.atoms, head.taskDocument) !== head.saved
      )
        cache.current.set(`${previous.repo}:${previous.episode}`, {
          atoms: head.atoms,
          task_document: head.taskDocument,
          saved: head.saved,
          review: head.review,
        });
      identity.current = { repo, episode };
      const request = ++generation.current;
      setReview(null);
      setTaskDocument(null);
      setError("");
      setSaving(false);
      setExporting(false);
      setSaved("");
      if (!repo.startsWith("bp/")) {
        setLoading(false);
        return;
      }
      setAtoms([]);
      clearSelection(null);
      setLoading(true);
      Promise.resolve(pendingSaves.current.get(`${repo}:${episode}`))
        .then(() => reviewRequest<Review>(reviewUrl(repo, episode)))
        .then((remote) => {
          if (request !== generation.current) return;
          const document = readTaskReview(remote);
          const initial = (remote.atoms || []).filter(
            (a) => a.style !== "subtask",
          );
          const snapshot = reviewSnapshot(initial, document);
          let pending = cache.current.get(`${repo}:${episode}`);
          try {
            const raw = sessionStorage.getItem(backupKey(repo, episode));
            const local = raw ? JSON.parse(raw) : null;
            if (
              !pending &&
              local?.revision === remote.revision &&
              local?.source_signature === remote.source_signature &&
              Array.isArray(local.atoms)
            ) {
              const restored = readTaskReview({
                ...remote,
                task_document: local.task_document,
                segments: taskReviewPayload(
                  remote,
                  local.atoms,
                  local.task_document,
                ).segments,
              });
              if (
                window.confirm(
                  "恢复本窗口尚未保存的任务标注？恢复后仍需点击“保存到服务器”。",
                )
              )
                pending = {
                  atoms: local.atoms,
                  task_document: restored,
                  review: remote,
                  saved: snapshot,
                };
            } else if (local && !pending) {
              setError(
                "本窗口有来源或版本已变化的旧草稿，备份未套用；请核对后编辑。",
              );
            }
            // Keep non-Subtask manual work from the previous workbench recoverable.
            const legacyRaw = sessionStorage.getItem(
              `bp-workbench-unsaved:v1:${repo}:${episode}`,
            );
            const legacy = legacyRaw ? JSON.parse(legacyRaw) : null;
            const oldRaw = sessionStorage.getItem(
              `lerobot-annotations:v2:${repo}::${episode}`,
            );
            const old = oldRaw ? JSON.parse(oldRaw) : null;
            const manual = (
              Array.isArray(legacy?.atoms)
                ? legacy.atoms
                : Array.isArray(old)
                  ? old
                  : []
            ).filter((a: LanguageAtom) => a.style !== "subtask");
            if (
              !pending &&
              !remote.atoms?.length &&
              manual.length &&
              window.confirm(
                "恢复原编辑器的手动/VQA 标注？Subtask 以当前服务端结果为准，旧备份保留。",
              )
            )
              pending = {
                atoms: manual,
                task_document: document,
                review: remote,
                saved: snapshot,
              };
          } catch {
            setError(
              "部分浏览器备份无法恢复，原备份保留；当前已加载服务端标注。",
            );
          }
          if (
            pending &&
            pending.review.revision === remote.revision &&
            pending.review.source_signature === remote.source_signature
          ) {
            setAtoms(pending.atoms);
            setTaskDocument(pending.task_document);
            setSaved(pending.saved);
          } else {
            setAtoms(initial);
            setTaskDocument(document);
            setSaved(snapshot);
            if (pending)
              setError(
                "此 Episode 的服务端版本已变化；未保存内容保留在窗口缓存中，请核对后再编辑。",
              );
          }
          setReview({ ...remote, repo, episode });
        })
        .catch((e) => {
          if (request === generation.current) setError(e.message);
        })
        .finally(() => {
          if (request === generation.current) setLoading(false);
        });
    },
    [setAtoms, clearSelection],
  );

  const save = async () => {
    if (!review || !taskDocument || loading)
      return { ok: false, error: error || "标注尚未加载完成" };
    const { repo, episode } = identity.current;
    const key = `${repo}:${episode}`;
    if (pendingSaves.current.has(key))
      return { ok: false, error: "正在保存，请稍候" };
    let release!: () => void;
    pendingSaves.current.set(
      key,
      new Promise<void>((resolve) => {
        release = resolve;
      }),
    );
    const request = generation.current;
    const snapshot = reviewSnapshot(atoms, taskDocument);
    setSaving(true);
    setError("");
    try {
      const result = await reviewRequest<{ revision: number }>(
        reviewUrl(repo, episode),
        taskReviewPayload(review, atoms, taskDocument),
      );
      if (request !== generation.current) {
        const pending = cache.current.get(key);
        if (pending)
          cache.current.set(key, {
            ...pending,
            saved: snapshot,
            review: { ...review, revision: result.revision },
          });
        try {
          if (
            pending &&
            reviewSnapshot(pending.atoms, pending.task_document) !== snapshot
          )
            sessionStorage.setItem(
              backupKey(repo, episode),
              JSON.stringify({
                atoms: pending.atoms,
                task_document: pending.task_document,
                revision: result.revision,
                source_signature: review.source_signature,
              }),
            );
          else sessionStorage.removeItem(backupKey(repo, episode));
        } catch {
          /* Optional backup; the Server save succeeded. */
        }
        return { ok: true };
      }
      setReview({ ...review, revision: result.revision, has_draft: true });
      setSaved(snapshot);
      cache.current.delete(key);
      return { ok: true, path: `Server 草稿 v${result.revision}` };
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e);
      if (request === generation.current) setError(message);
      return { ok: false, error: message };
    } finally {
      pendingSaves.current.delete(key);
      release();
      if (request === generation.current) setSaving(false);
    }
  };

  const exportDataset = async () => {
    if (!review || !taskDocument || loading)
      throw new Error(error || "标注尚未加载完成");
    if (reviewSnapshot(atoms, taskDocument) !== saved)
      throw new Error("请先保存到服务器，再导出修订数据集");
    if (exporting) throw new Error("正在导出，请稍候");
    const base = reviewUrl(identity.current.repo);
    const request = generation.current;
    setExporting(true);
    try {
      const job = await reviewRequest<{ id: string }>(`${base}/exports`, {
        revision: review.revision,
        source_signature: review.source_signature,
        name: null,
      });
      for (let i = 0; i < 720; i++) {
        const status = await reviewRequest<{
          state: string;
          error?: string;
          dataset_name: string;
        }>(`${base}/exports/${job.id}`);
        if (request !== generation.current)
          throw new Error("已切换 Episode；导出仍在后台执行，请在工作台查看");
        if (status.state === "CONVERTED")
          return {
            output_dir: status.dataset_name,
            persistent_rows:
              taskDocument.attempts.length +
              atoms.filter(
                (a) => a.style !== "vqa" && a.style !== "interjection",
              ).length,
            event_rows: atoms.filter(
              (a) => a.style === "vqa" || a.style === "interjection",
            ).length,
          };
        if (!["QUEUED", "RUNNING"].includes(status.state))
          throw new Error(status.error || `修订导出 ${status.state}`);
        await new Promise((resolve) => setTimeout(resolve, 2500));
      }
      throw new Error("导出仍在后台执行，请在工作台任务列表查看");
    } finally {
      if (request === generation.current) setExporting(false);
    }
  };

  const normalize = (atom: LanguageAtom) => atom;
  return {
    load,
    save,
    exportDataset,
    normalize,
    loading,
    saving,
    exporting,
    error,
    review,
    taskDocument,
    setTaskDocument,
    ready: !!review && !!taskDocument && !loading,
    dirty,
  };
}
