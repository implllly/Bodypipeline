"use client";

import Link from "next/link";
import React, { useEffect, useMemo, useState } from "react";
import { useFlaggedEpisodes } from "@/context/flagged-episodes-context";
import { EpisodeScoreBadge, EpisodeScorePanel, useEpisodeScores } from "@/components/episode-scores";

import type { DatasetDisplayInfo } from "@/app/[org]/[dataset]/[episode]/fetch-data";

interface SidebarProps {
  datasetInfo: DatasetDisplayInfo;
  paginatedEpisodes: number[];
  episodeId: number;
  totalPages: number;
  currentPage: number;
  prevPage: () => void;
  nextPage: () => void;
  showFlaggedOnly: boolean;
  onShowFlaggedOnlyChange: (v: boolean) => void;
  onEpisodeSelect?: (ep: number) => void;
}

function TrashIcon() {
  return <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M3 6h18M9 6V4h6v2M5 6l1 14h12l1-14M10 10v6M14 10v6" /></svg>;
}

const Sidebar: React.FC<SidebarProps> = ({
  datasetInfo,
  paginatedEpisodes,
  episodeId,
  totalPages,
  currentPage,
  prevPage,
  nextPage,
  showFlaggedOnly,
  onShowFlaggedOnlyChange,
  onEpisodeSelect,
}) => {
  const [mobileVisible, setMobileVisible] = useState(false);
  const { flagged, count, toggle, remap, storageError } = useFlaggedEpisodes();
  const { result: scores, retry: retryScores } = useEpisodeScores(datasetInfo.repoId);
  const scoreByEpisode = useMemo(() => new Map(scores.report?.episodes.map(row => [row.episode_index, row]) || []), [scores.report]);
  const [recycle, setRecycle] = useState<{entries: {episode: number}[]; revision: number; total_episodes: number; operation?: {id: string; state: string; mapping?: Record<string, number>}} | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [undo, setUndo] = useState<number | null>(null);
  const api = `/api/recycle/${encodeURIComponent(datasetInfo.repoId.split("/")[1])}`;
  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const response = await fetch(api, {cache: "no-store"});
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "无法读取回收站");
        if (active) {
          const operation = result.operation;
          if (operation?.state === "completed") {
            const key = `recycle-generation:${datasetInfo.repoId}`;
            if (sessionStorage.getItem(key) !== operation.id) {
              remap(operation.mapping || {}, operation.id);
              sessionStorage.setItem(key, operation.id);
              window.location.reload();
              return;
            }
          }
          setRecycle(result);
        }
      } catch (error) { if (active) setNotice(String(error instanceof Error ? error.message : error)); }
    };
    void load();
    const timer = setInterval(load, 5000);
    window.addEventListener("focus", load);
    return () => { active = false; clearInterval(timer); window.removeEventListener("focus", load); };
  }, [api, datasetInfo.repoId, remap]);
  const trashed = useMemo(() => new Set(recycle?.entries.map(e => e.episode) || []), [recycle]);
  const cleaning = ["pending", "running"].includes(recycle?.operation?.state || "");
  async function changeEpisode(episode: number, action: "trash" | "restore") {
    if (!recycle || busy || cleaning) return;
    setBusy(true);
    try {
      const response = await fetch(`${api}/${action}`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({episodes: [episode], revision: recycle.revision})});
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "操作失败，请刷新重试");
      setRecycle(result);
      setNotice(action === "trash" ? `Episode ${episode} 已放入回收站` : `Episode ${episode} 已恢复`);
      setUndo(action === "trash" ? episode : null);
      if (action === "trash" && episode === episodeId) {
        const removed = new Set<number>(result.entries.map((e: {episode: number}) => e.episode));
        const next = Array.from({length: datasetInfo.total_episodes}, (_, i) => i).find(i => !removed.has(i) && i > episode) ?? Array.from({length: datasetInfo.total_episodes}, (_, i) => i).find(i => !removed.has(i));
        window.location.assign(next === undefined ? `/${datasetInfo.repoId}/trash` : `/${datasetInfo.repoId}/episode_${next}`);
      }
    } catch (error) { setNotice(error instanceof Error ? error.message : "操作失败"); }
    finally { setBusy(false); }
  }

  const displayEpisodes = useMemo(() => {
    const episodes = !showFlaggedOnly || count === 0 ? paginatedEpisodes : [...flagged].sort((a, b) => a - b);
    return episodes.filter(episode => !trashed.has(episode));
  }, [paginatedEpisodes, showFlaggedOnly, flagged, count, trashed]);

  return (
    <div className="flex z-10 shrink-0">
      <nav
        className={`shrink-0 overflow-y-auto bg-[var(--surface-0)] border-r border-white/5 p-4 break-words w-60 ${
          mobileVisible ? "block" : "hidden"
        } md:block`}
        aria-label="数据集导航"
      >
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs text-slate-400 tabular">
          <dt className="uppercase tracking-wide text-[10px] text-slate-500">
            帧数
          </dt>
          <dd className="text-slate-200">
            {datasetInfo.total_frames.toLocaleString()}
          </dd>
          <dt className="uppercase tracking-wide text-[10px] text-slate-500">
            Episode 数
          </dt>
          <dd className="text-slate-200">
            {datasetInfo.total_episodes.toLocaleString()}
          </dd>
          <dt className="uppercase tracking-wide text-[10px] text-slate-500">
            FPS
          </dt>
          <dd className="text-slate-200">{datasetInfo.fps}</dd>
        </dl>

        <EpisodeScorePanel result={scores} episodeId={episodeId} retry={retryScores} />

        <a href={`/${datasetInfo.repoId}/batch`} className="pipeline-batch-link"><span aria-hidden="true">▦</span> 批量视频预览</a>
        {storageError && <p role="alert" className="pipeline-recycle-notice">{storageError}</p>}

        <a href={`/${datasetInfo.repoId}/trash`} className="pipeline-recycle-link"><TrashIcon /> 回收站 <span>{recycle?.entries.length ?? "…"}</span></a>
        {notice && <div role="status" className="pipeline-recycle-notice">{notice} {undo !== null && <button disabled={busy || cleaning} onClick={() => changeEpisode(undo, "restore")}>撤销</button>}</div>}
        {cleaning && <p className="pipeline-recycle-notice">正在永久清理，请等待完成后刷新。</p>}
        <div className="mt-5 flex items-center justify-between">
          <p className="text-[10px] uppercase tracking-wide text-slate-500">
            Episode 列表
          </p>
          {count > 0 && (
            <button
              onClick={() => onShowFlaggedOnlyChange(!showFlaggedOnly)}
              className={`text-[10px] uppercase tracking-wide px-2 py-0.5 rounded-md transition-colors ${
                showFlaggedOnly
                  ? "bg-orange-500/15 text-orange-300 border border-orange-500/30"
                  : "text-slate-500 hover:text-slate-300 border border-white/10"
              }`}
            >
              已标记 · {count}
            </button>
          )}
        </div>

        <ul className="mt-2 space-y-px">
          {displayEpisodes.map((episode) => {
            const active = episode === episodeId;
            const itemClass = `group flex items-center justify-between gap-2 px-2 py-1 rounded-md text-xs tabular transition-colors ${
              active
                ? "bg-cyan-400/10 text-cyan-300"
                : "text-slate-300 hover:bg-white/5"
            }`;
            return (
              <li key={episode}>
                {onEpisodeSelect ? (
                  <div className={itemClass}>
                    <button
                      onClick={() => onEpisodeSelect(episode)}
                      className="flex-1 text-left"
                    >
                      Episode {episode}
                    </button>
                    <EpisodeScoreBadge score={scoreByEpisode.get(episode)} />
                    <button type="button" className="pipeline-trash-button" title={`将 Episode ${episode} 放入回收站`} aria-label={`将 Episode ${episode} 放入回收站`} disabled={!recycle || busy || cleaning} onClick={() => changeEpisode(episode, "trash")}><TrashIcon /></button>
                    <button
                      onClick={() => toggle(episode)}
                      className={`text-xs leading-none transition-colors ${
                        flagged.has(episode)
                          ? "text-orange-400 hover:text-orange-300"
                          : "text-slate-600 hover:text-slate-400 opacity-0 group-hover:opacity-100"
                      }`}
                      title={flagged.has(episode) ? "取消标记" : "标记"}
                    >
                      ⚑
                    </button>
                  </div>
                ) : (
                  <div className={itemClass}>
                    <Link
                      href={`./episode_${episode}`}
                      className="flex-1 text-left"
                    >
                      Episode {episode}
                    </Link>
                    <EpisodeScoreBadge score={scoreByEpisode.get(episode)} />
                    <button type="button" className="pipeline-trash-button" title={`将 Episode ${episode} 放入回收站`} aria-label={`将 Episode ${episode} 放入回收站`} disabled={!recycle || busy || cleaning} onClick={() => changeEpisode(episode, "trash")}><TrashIcon /></button>
                    <button
                      onClick={() => toggle(episode)}
                      className={`text-xs leading-none transition-colors ${
                        flagged.has(episode)
                          ? "text-orange-400 hover:text-orange-300"
                          : "text-slate-600 hover:text-slate-400 opacity-0 group-hover:opacity-100"
                      }`}
                      title={flagged.has(episode) ? "取消标记" : "标记"}
                    >
                      ⚑
                    </button>
                  </div>
                )}
              </li>
            );
          })}
        </ul>

        {!showFlaggedOnly && totalPages > 1 && (
          <div className="mt-3 flex items-center gap-2 text-[10px] uppercase tracking-wide text-slate-400">
            <button
              onClick={prevPage}
              className={`px-2 py-1 rounded-md border border-white/10 transition-colors hover:bg-white/5 hover:text-slate-200 ${
                currentPage === 1 ? "cursor-not-allowed opacity-40" : ""
              }`}
              disabled={currentPage === 1}
            >
              ‹ 上一页
            </button>
            <span className="tabular text-slate-500">
              {currentPage} / {totalPages}
            </span>
            <button
              onClick={nextPage}
              className={`ml-auto px-2 py-1 rounded-md border border-white/10 transition-colors hover:bg-white/5 hover:text-slate-200 ${
                currentPage === totalPages
                  ? "cursor-not-allowed opacity-40"
                  : ""
              }`}
              disabled={currentPage === totalPages}
            >
              下一页 ›
            </button>
          </div>
        )}
      </nav>

      <button
        className="mx-1 flex items-center opacity-50 hover:opacity-100 focus:outline-none focus:ring-0 md:hidden"
        onClick={() => setMobileVisible((prev) => !prev)}
        title="展开或收起侧边栏"
      >
        <div className="h-10 w-1 rounded-full bg-white/20" />
      </button>
    </div>
  );
};

export default Sidebar;
