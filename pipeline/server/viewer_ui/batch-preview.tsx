"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { ThemeToggle } from "@/components/theme-toggle";
import {
  FlaggedEpisodesProvider,
  useFlaggedEpisodes,
} from "@/context/flagged-episodes-context";
import { loadBatchCatalog } from "@/utils/batch-preview-data";
import {
  GRID_SIZES,
  batchPage,
  clipBounds,
  clipPosition,
  type BatchCatalog,
  type BatchClip,
  type GridSize,
} from "@/utils/batch-preview-model";

type Report = {
  progress: number;
  ready: boolean;
  ended: boolean;
  error: boolean;
};
type Seek = { id: number; progress: number };
const time = (value: number) =>
  `${Math.floor(value / 60)}:${Math.floor(value % 60)
    .toString()
    .padStart(2, "0")}`;
const message = (error: unknown) =>
  error instanceof Error ? error.message : String(error);

function VideoCard({
  clip,
  episode,
  repoId,
  playing,
  rate,
  seek,
  focused,
  onFocus,
  onReport,
}: {
  clip?: BatchClip;
  episode: number;
  repoId: string;
  playing: boolean;
  rate: number;
  seek: Seek;
  focused: boolean;
  onFocus: (episode: number | null) => void;
  onReport: (episode: number, report: Report) => void;
}) {
  const { has, toggle } = useFlaggedEpisodes();
  const video = useRef<HTMLVideoElement>(null);
  const article = useRef<HTMLElement>(null);
  const command = useRef({ playing, rate, seek });
  command.current = { playing, rate, seek };
  const [ready, setReady] = useState(false);
  const [error, setError] = useState(clip ? "" : "该 Episode 缺少此视角");
  const [elapsed, setElapsed] = useState(0);
  const [ended, setEnded] = useState(false);
  const [duration, setDuration] = useState(0);

  const report = useCallback(() => {
    const element = video.current;
    if (!clip || !element || !Number.isFinite(element.duration)) return;
    const [start, end] = clipBounds(clip, element.duration);
    const position = Math.max(0, element.currentTime - start);
    const complete = element.currentTime >= end - 0.001;
    if (complete) {
      element.pause();
      if (element.currentTime > end + 0.001) element.currentTime = end;
    }
    setElapsed(position);
    setEnded(complete);
    onReport(episode, {
      ready: true,
      error: false,
      ended: complete,
      progress: end > start ? Math.min(1, position / (end - start)) : 1,
    });
  }, [clip, episode, onReport]);
  const play = useCallback(() => {
    const element = video.current;
    if (!element || !clip || !Number.isFinite(element.duration)) return;
    const [, end] = clipBounds(clip, element.duration);
    if (element.currentTime >= end - 0.001) return;
    void element
      .play()
      .then(() => setError(""))
      .catch((err) => {
        if (err.name === "AbortError") return;
        setError(
          err.name === "NotAllowedError"
            ? "播放被浏览器阻止，请再点一次全部播放"
            : "视频无法播放，请重试或单条查看",
        );
        onReport(episode, {
          progress: 0,
          ready: true,
          ended: false,
          error: true,
        });
      });
  }, [clip, episode, onReport]);
  const position = useCallback(
    (progress: number) => {
      const element = video.current;
      if (!element || !clip || !Number.isFinite(element.duration)) return;
      element.currentTime = clipPosition(clip, element.duration, progress);
      setEnded(progress >= 1);
      report();
    },
    [clip, report],
  );

  useEffect(() => {
    if (!ready) return;
    position(seek.progress);
    if (playing) play();
    // A new seek request applies once; play/pause must preserve per-video time.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seek.id, ready, position]);
  useEffect(() => {
    if (!ready) return;
    const element = video.current!;
    element.playbackRate = rate;
    if (playing) play();
    else element.pause();
  }, [playing, rate, ready, play]);
  useEffect(() => {
    onReport(episode, {
      progress: 0,
      ready: false,
      ended: false,
      error: !clip,
    });
    const element = video.current;
    // React development StrictMode replays effects while retaining the node.
    // Reattach the source after its cleanup, as well as releasing it on unmount.
    if (element && clip && element.getAttribute("src") !== clip.url) {
      element.src = clip.url;
      element.load();
    }
    return () => {
      element?.pause();
      if (element) {
        element.removeAttribute("src");
        element.load();
      }
      onReport(episode, {
        progress: 0,
        ready: false,
        ended: false,
        error: false,
      });
    };
  }, [clip, episode, onReport]);
  useEffect(() => {
    const element = video.current;
    if (!element || !playing || !ready) return;
    // Frame callbacks stop v3 shared files at THIS episode's boundary.
    let id = 0;
    let active = true;
    const tick = () => {
      if (!active) return;
      if (
        clip &&
        element.currentTime >= clipBounds(clip, element.duration)[1] - 0.001
      )
        report();
      id = element.requestVideoFrameCallback(tick);
    };
    if (element.requestVideoFrameCallback)
      id = element.requestVideoFrameCallback(tick);
    const timer =
      typeof element.requestVideoFrameCallback === "function"
        ? undefined
        : window.setInterval(report, 40);
    return () => {
      active = false;
      if (id) element.cancelVideoFrameCallback(id);
      clearInterval(timer);
    };
  }, [playing, ready, report, clip]);
  useEffect(() => {
    if (!focused) return;
    const previous = document.activeElement as HTMLElement | null;
    const root = article.current!;
    root.querySelector<HTMLButtonElement>(".batch-close")?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onFocus(null);
      }
      if (event.key === "Tab") {
        const elements = [...root.querySelectorAll<HTMLElement>("button, a")];
        const first = elements[0],
          last = elements.at(-1);
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", keydown);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", keydown);
      document.body.style.overflow = overflow;
      previous?.focus();
    };
  }, [focused, onFocus]);

  return (
    <article
      ref={article}
      className={`batch-card ${has(episode) ? "is-flagged" : ""} ${focused ? "is-focused" : ""}`}
      data-episode={episode}
      role={focused ? "dialog" : undefined}
      aria-modal={focused || undefined}
      aria-label={`Episode ${episode} 视频`}
    >
      <header>
        <strong>Episode {episode.toString().padStart(3, "0")}</strong>
        <button
          type="button"
          className={`batch-flag ${has(episode) ? "active" : ""}`}
          aria-pressed={has(episode)}
          title={has(episode) ? "已标记待检查，点击取消" : "标为待检查"}
          aria-label={`${has(episode) ? "取消" : "标为"}待检查 Episode ${episode}`}
          onClick={() => toggle(episode)}
        >
          {has(episode) ? "⚑ 已标记" : "⚐ 待检查"}
        </button>
        {focused && (
          <button
            type="button"
            className="batch-close"
            onClick={() => onFocus(null)}
          >
            收起 Esc
          </button>
        )}
      </header>
      <div
        className="batch-stage"
        onDoubleClick={() => onFocus(focused ? null : episode)}
      >
        {clip && (
          <video
            ref={video}
            src={clip.url}
            muted
            playsInline
            preload="metadata"
            onLoadedMetadata={() => {
              const element = video.current!;
              if (
                !Number.isFinite(element.duration) ||
                element.duration <= clip.start
              ) {
                setError("视频时长与 Episode 元数据不匹配");
                onReport(episode, {
                  progress: 0,
                  ready: false,
                  ended: false,
                  error: true,
                });
                return;
              }
              setDuration(
                Math.min(clip.end ?? element.duration, element.duration) -
                  clip.start,
              );
              setError("");
              element.playbackRate = command.current.rate;
              setReady(true);
            }}
            onTimeUpdate={report}
            onSeeked={() => {
              report();
              if (command.current.playing) play();
            }}
            onEnded={report}
            onError={() => {
              setError("视频加载失败，请单条查看或刷新重试");
              onReport(episode, {
                progress: 0,
                ready: false,
                ended: false,
                error: true,
              });
            }}
          />
        )}
        {(!ready || error) && (
          <div className={`batch-video-state ${error ? "is-error" : ""}`}>
            {error || "正在加载视频…"}
          </div>
        )}
        {ended && !error && <span className="batch-ended">已播完</span>}
        {!focused && (
          <button
            type="button"
            className="batch-expand"
            aria-label={`放大 Episode ${episode}`}
            onClick={() => onFocus(episode)}
          >
            ⛶
          </button>
        )}
      </div>
      <div className="batch-card-progress">
        <span
          style={{
            width: `${duration ? Math.min(100, (elapsed / duration) * 100) : 0}%`,
          }}
        />
      </div>
      <footer>
        <span>
          {time(elapsed)} <small>/ {ready ? time(duration) : "--:--"}</small>
        </span>
        <a
          href={`/${repoId}/episode_${episode}`}
          onClick={() => {
            video.current?.pause();
            try {
              sessionStorage.setItem("activeTab", "annotations");
            } catch {
              /* optional preference */
            }
          }}
        >
          单条标注 ↗
        </a>
      </footer>
    </article>
  );
}

function BatchWall({ repoId }: { repoId: string }) {
  const flags = useFlaggedEpisodes();
  const [catalog, setCatalog] = useState<BatchCatalog | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [generation, setGeneration] = useState(0);
  const [camera, setCamera] = useState("");
  const [size, setSize] = useState<GridSize>(9);
  const [page, setPage] = useState(0);
  const [onlyFlagged, setOnlyFlagged] = useState(false);
  const [trashed, setTrashed] = useState<Set<number>>(new Set());
  const [playing, setPlaying] = useState(false);
  const [rate, setRate] = useState(1);
  const [seek, setSeek] = useState<Seek>({ id: 0, progress: 0 });
  const [progress, setProgress] = useState(0);
  const [status, setStatus] = useState({ ready: 0, ended: 0, failed: 0 });
  const [focused, setFocused] = useState<number | null>(null);
  const reports = useRef(new Map<number, Report>());
  const saved = useRef(false);
  const visibleRef = useRef<number[]>([]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    setPlaying(false);
    const recycle = repoId.startsWith("bp/")
      ? fetch(`/api/recycle/${encodeURIComponent(repoId.split("/")[1])}`, {
          cache: "no-store",
        }).then(async (response) => {
          const data = await response.json();
          if (!response.ok)
            throw new Error(data.detail || "回收站状态读取失败");
          if (["pending", "running"].includes(data.operation?.state))
            throw new Error("数据集正在清理，请完成后刷新预览");
          if (data.operation?.state === "completed" && data.operation.mapping)
            flags.remap(data.operation.mapping, data.operation.id);
          return new Set<number>(
            data.entries.map((entry: { episode: number }) => entry.episode),
          );
        })
      : Promise.resolve(new Set<number>());
    void Promise.all([loadBatchCatalog(repoId), recycle])
      .then(([data, removed]) => {
        if (!active) return;
        setCatalog(data);
        setTrashed(removed);
        setCamera((previous) =>
          data.cameras.includes(previous) ? previous : data.cameras[0],
        );
        try {
          const preference = JSON.parse(
            sessionStorage.getItem(`batch-view:${repoId}`) || "null",
          );
          if (preference && !saved.current) {
            if (GRID_SIZES.includes(preference.size)) setSize(preference.size);
            if (data.cameras.includes(preference.camera))
              setCamera(preference.camera);
            if (Number.isSafeInteger(preference.page) && preference.page >= 0)
              setPage(preference.page);
            if ([0.5, 1, 1.5, 2, 4].includes(preference.rate))
              setRate(preference.rate);
            setOnlyFlagged(Boolean(preference.onlyFlagged));
          }
          saved.current = true;
        } catch {
          /* preference is optional */
        }
      })
      .catch((err) => {
        if (active) setError(message(err));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [repoId, generation, flags.remap]);

  useEffect(() => {
    if (!catalog || !saved.current) return;
    try {
      sessionStorage.setItem(
        `batch-view:${repoId}`,
        JSON.stringify({ size, camera, page, rate, onlyFlagged }),
      );
    } catch {
      /* optional */
    }
  }, [catalog, repoId, size, camera, page, rate, onlyFlagged]);
  const episodes =
    catalog?.episodes.filter(
      (id) => !trashed.has(id) && (!onlyFlagged || flags.has(id)),
    ) || [];
  const pages = Math.max(1, Math.ceil(episodes.length / size));
  const currentPage = Math.min(page, pages - 1);
  const visible = batchPage(episodes, currentPage, size);
  visibleRef.current = visible;
  const batchKey = `${generation}:${size}:${camera}:${visible.join(",")}`;
  useEffect(() => {
    setPage((current) => Math.min(current, pages - 1));
  }, [pages]);
  useEffect(() => {
    setPlaying(false);
    setFocused(null);
    setProgress(0);
    setStatus({ ready: 0, ended: 0, failed: 0 });
    setSeek((previous) => ({ id: previous.id + 1, progress: 0 }));
  }, [batchKey]);
  const onReport = useCallback((episode: number, report: Report) => {
    reports.current.set(episode, report);
  }, []);
  useEffect(() => {
    const timer = setInterval(() => {
      const states = visibleRef.current.map((id) => reports.current.get(id));
      const valid = states.filter((row): row is Report =>
        Boolean(row && row.ready && !row.error),
      );
      setStatus({
        ready: valid.length,
        ended: valid.filter((row) => row.ended).length,
        failed: states.filter((row) => row?.error).length,
      });
      if (valid.length)
        setProgress(Math.min(...valid.map((row) => row.progress)));
      if (
        states.length &&
        states.every((row) => row && (row.error || row.ended))
      )
        setPlaying(false);
    }, 250);
    const hide = () => {
      if (document.hidden) setPlaying(false);
    };
    document.addEventListener("visibilitychange", hide);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", hide);
    };
  }, []);
  // Pause before returning to a tab after other users changed/deleted episodes.
  useEffect(() => {
    const focus = () => {
      setPlaying(false);
      setGeneration((previous) => previous + 1);
    };
    window.addEventListener("focus", focus);
    return () => window.removeEventListener("focus", focus);
  }, []);
  function seekAll(value: number) {
    setProgress(value);
    setSeek((previous) => ({ id: previous.id + 1, progress: value }));
  }

  return (
    <main className="batch-preview">
      <header className="batch-heading">
        <div>
          <a
            className="batch-back"
            href={`/${repoId}/episode_${visible[0] ?? 0}`}
          >
            ← 单条预览
          </a>
          <h1>批量视频预览</h1>
          <p>
            {repoId} <span>· 同批 Episode，统一视角快速巡检</span>
          </p>
        </div>
        <ThemeToggle />
      </header>
      <section className="batch-toolbar" aria-label="批量播放控制">
        <div className="batch-toolbar-row">
          <div className="batch-layout-options" aria-label="每批视频数量">
            {GRID_SIZES.map((value) => (
              <button
                key={value}
                type="button"
                aria-pressed={size === value}
                onClick={() => {
                  setSize(value);
                  setPage(0);
                }}
              >
                {value} 路
              </button>
            ))}
          </div>
          <label>
            视角{" "}
            <select
              aria-label="统一视频视角"
              value={camera}
              disabled={!catalog || loading}
              onChange={(event) => setCamera(event.target.value)}
            >
              {catalog?.cameras.map((key) => (
                <option key={key} value={key}>
                  {key.replace(/^observation\.images?\./, "")}
                </option>
              ))}
            </select>
          </label>
          <label>
            倍速{" "}
            <select
              aria-label="统一播放倍速"
              value={rate}
              onChange={(event) => setRate(Number(event.target.value))}
            >
              {[0.5, 1, 1.5, 2, 4].map((value) => (
                <option key={value} value={value}>
                  {value}×
                </option>
              ))}
            </select>
          </label>
          <label className="batch-filter">
            <input
              type="checkbox"
              checked={onlyFlagged}
              onChange={(event) => {
                setOnlyFlagged(event.target.checked);
                setPage(0);
              }}
            />
            仅待检查 <span>{flags.count}</span>
          </label>
        </div>
        <div className="batch-toolbar-row">
          <button
            className="batch-primary"
            type="button"
            disabled={loading || !!error || !visible.length}
            onClick={() => {
              if (!playing && progress >= 0.999) seekAll(0);
              setPlaying((previous) => !previous);
            }}
          >
            {playing ? "Ⅱ 全部暂停" : "▶ 全部播放"}
          </button>
          <button
            type="button"
            disabled={!visible.length || loading}
            onClick={() => {
              seekAll(0);
              setPlaying(true);
            }}
          >
            从头播放
          </button>
          <label className="batch-seek">
            整批定位{" "}
            <input
              type="range"
              aria-label="整批百分比进度"
              min="0"
              max="1000"
              value={Math.round(progress * 1000)}
              disabled={!visible.length || loading}
              onChange={(event) => seekAll(Number(event.target.value) / 1000)}
            />
            <output>{Math.round(progress * 100)}%</output>
          </label>
          <div className="batch-pagination">
            <button
              type="button"
              disabled={currentPage === 0 || loading}
              onClick={() => setPage((previous) => previous - 1)}
            >
              ← 上一批
            </button>
            <span>
              {currentPage + 1} / {pages}
            </span>
            <button
              type="button"
              disabled={currentPage >= pages - 1 || loading}
              onClick={() => setPage((previous) => previous + 1)}
            >
              下一批 →
            </button>
          </div>
        </div>
      </section>
      <div className="batch-summary">
        <span>
          {loading
            ? "正在读取视频目录…"
            : `${episodes.length} 条数据 · 本批 ${visible.length} 路 · 就绪 ${status.ready} · 播完 ${status.ended}${status.failed ? ` · 加载失败 ${status.failed}` : ""}`}
        </span>
        <span>静音播放 · 拖动按各自时长定位 · 双击放大</span>
      </div>
      {flags.storageError && (
        <p role="alert" className="batch-notice">
          {flags.storageError}
        </p>
      )}
      {error ? (
        <div className="batch-empty" role="alert">
          <strong>预览暂不可用</strong>
          <p>{error}</p>
          <button onClick={() => setGeneration((previous) => previous + 1)}>
            重新加载
          </button>
        </div>
      ) : loading ? (
        <div className="batch-empty">正在读取 Episode 与视角信息…</div>
      ) : !visible.length ? (
        <div className="batch-empty">
          <strong>
            {onlyFlagged ? "还没有待检查的数据" : "当前没有可预览的 Episode"}
          </strong>
          <p>
            {onlyFlagged
              ? "在视频右上角点「待检查」，即可在这里集中查看。"
              : "请检查数据集或回收站。"}
          </p>
          {onlyFlagged && (
            <button onClick={() => setOnlyFlagged(false)}>查看全部数据</button>
          )}
        </div>
      ) : (
        <div className={`batch-grid batch-grid-${size}`} key={batchKey}>
          {visible.map((episode) => (
            <VideoCard
              key={`${camera}:${episode}`}
              clip={catalog!.clips[camera]?.[episode]}
              episode={episode}
              repoId={repoId}
              playing={playing}
              rate={rate}
              seek={seek}
              focused={focused === episode}
              onFocus={setFocused}
              onReport={onReport}
            />
          ))}
        </div>
      )}
      {focused !== null && (
        <div
          className="batch-focus-backdrop"
          onClick={() => setFocused(null)}
        />
      )}
      <p className="batch-footnote">
        待检查标记保存在当前浏览器，单条预览与批量预览共用。每条播放结束后停在最后一帧，切换批次或视角会暂停并从头准备。
      </p>
    </main>
  );
}

export default function BatchPreview({ repoId }: { repoId: string }) {
  return (
    <FlaggedEpisodesProvider repoId={repoId}>
      <BatchWall repoId={repoId} />
    </FlaggedEpisodesProvider>
  );
}
