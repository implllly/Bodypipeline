"""Boundary idle-frame trimming for registered LeRobot datasets.

The User-facing contract is one derived dataset per preview. Detection runs in
its own lightweight queue (``trim_preview`` records) because scanning every
episode does not need a Slurm allocation and must not block conversion jobs.
Execution reuses the serial job queue (``operation == "stationary_trim"``) so
allocation handling, shutdown, deduplication and dataset registration match the
existing version conversion and subtask export flows.

Robot-specific signal semantics and thresholds come from
``RobotProvider.stationary_trim_policy``; the page and these tasks never branch
on a robot model.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

from pipeline.shared.collections import dataset_name
from pipeline.shared.core import DomainError, atomic_json, digest, executable_path, now

from . import static_intervals
from .datasets import DATA_READY_STATES, TARGET_FORMATS, dataset_for_job
from .merges import source_snapshot
from .quality import DOCTOR_CHECK_IDS, doctor_gate
from .recycle import dataset_lock
from .stationary_trim import ALGORITHM, normalize_policy, resolve_policy
from .storage import ROBOT_LAYOUT, data_directory, share_data_tree, storage_path

NAME_SUFFIX = " · 首尾裁剪"
DEDUP_STATES = {
    "FAILED",
    "INTERRUPTED",
    "BLOCKED",
    "DELETING",
    "DELETE_FAILED",
    "DELETED",
}
ACTIVE = {"QUEUED", "RUNNING"}
PREVIEW_STATES = {"QUEUED", "RUNNING", "COMPLETED", "FAILED", "INTERRUPTED"}


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class TrimRunner:
    def __init__(self, settings, runner):
        self.settings, self.runner, self.store = settings, runner, runner.store
        self.lock = runner.lock

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _key(path) -> str:
        return hashlib.sha256(str(path).encode()).hexdigest()

    def _tool(self) -> tuple[str, str]:
        tool = executable_path(self.settings.letools_bin)
        if not tool:
            raise DomainError("首尾静止帧裁剪需要可执行的 LeTools 环境", 503)
        python = Path(tool).with_name("python")
        if not python.is_file():
            raise DomainError("找不到 LeTools Python 运行环境", 503)
        return tool, str(python)

    def provider_policy(self, job, hook: str = "stationary_trim_policy") -> dict:
        """Trusted Provider configuration; absence means "use dataset units"."""

        try:
            runtime = self.runner.runtime_for_job(job)
            provider = self.runner.catalog.provider(runtime)
        except (DomainError, KeyError, TypeError, ValueError):
            return {}
        getter = getattr(provider, hook, None)
        if not callable(getter):
            # Providers that only declare the boundary hook still describe
            # their motion signal in the Profile; reuse it instead of falling
            # back to per-second unit thresholds they never verified.
            if hook == "stationary_interval_policy":
                from .providers.base import configured_interval_policy

                return configured_interval_policy(runtime.profile)
            return {}
        try:
            value = getter(runtime.profile)
        except (DomainError, TypeError, ValueError, AttributeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _interval_policy(self, job, info: dict) -> dict:
        policy = static_intervals.normalize_policy(
            self.provider_policy(job, "stationary_interval_policy")
        )
        return resolve_policy(policy, info.get("features") or {}, info.get("fps"))

    def _policy(self, job, info: dict, overrides: dict | None = None) -> dict:
        base = normalize_policy(self.provider_policy(job))
        overrides = dict(overrides or {})
        scale = overrides.pop("threshold_scale", None)
        applied = {**base}
        for key, value in overrides.items():
            if value is not None:
                applied[key] = value
        policy = normalize_policy(applied)
        resolved = resolve_policy(policy, info.get("features") or {}, info.get("fps"))
        if scale is not None:
            if (
                isinstance(scale, bool)
                or not isinstance(scale, (int, float))
                or not 0.01 <= float(scale) <= 100
            ):
                raise DomainError("threshold_scale 必须在 0.01–100 之间", 400)
            resolved["thresholds"] = [
                None if value is None else value * float(scale)
                for value in resolved["thresholds"]
            ]
        return resolved

    def query_policy(self, job_id: str) -> dict:
        """Capabilities and defaults the page shows before a preview exists."""

        job = self.store.get("job", job_id)
        try:
            source = dataset_for_job(self.settings, job)
            info = _read_json(source / "meta/info.json")
            policy = self._policy(job, info)
            self._check_recycle(source)
            self._tool()
        except (DomainError, OSError, ValueError, TypeError, KeyError) as exc:
            return {"available": False, "reason": str(exc)}
        return {
            "available": True,
            "reason": "",
            "algorithm": ALGORITHM,
            "feature": policy["feature"],
            "units": policy["units"],
            "thresholds": policy["thresholds"],
            "threshold_per_frame": policy["threshold_per_frame"],
            "defaults": {
                "trim_start": policy["trim_start"],
                "trim_end": policy["trim_end"],
                "min_idle_seconds": policy["min_idle_seconds"],
                "min_active_seconds": policy["min_active_seconds"],
                "padding_before_seconds": policy["padding_before_seconds"],
                "padding_after_seconds": policy["padding_after_seconds"],
                "remove_fully_static": policy["remove_fully_static"],
                "threshold_scale": 1.0,
            },
            "fps": info.get("fps"),
            "episode_count": info.get("total_episodes"),
            "frame_count": info.get("total_frames"),
        }

    def _check_recycle(self, path: Path) -> None:
        try:
            row = self.store.get("recycle", self._key(path))
        except DomainError as exc:
            if exc.code == 404:
                return
            raise
        if (row.get("operation") or {}).get("state") in {"running", "pending"}:
            raise DomainError("来源正在永久清理，请等待完成后再裁剪")
        if row.get("entries"):
            raise DomainError("来源存在回收站 Episode，请先还原或永久清理再裁剪")

    def _describe(self, job) -> dict:
        source = dataset_for_job(self.settings, job)
        info = _read_json(source / "meta/info.json")
        version = info.get("codebase_version")
        if version not in TARGET_FORMATS:
            raise ValueError("仅支持 LeRobot v2.1 / v3.0")
        episodes, frames = info.get("total_episodes"), info.get("total_frames")
        fps = info.get("fps")
        if any(type(value) is not int or value <= 0 for value in (episodes, frames)):
            raise ValueError("Episode / 帧数必须是正整数")
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or fps <= 0:
            raise ValueError("FPS 必须是正数")
        policy = self._policy(job, info)
        self._check_recycle(source)
        return {
            "dataset_path": str(source),
            "source_format": version,
            "fps": float(fps),
            "episode_count": episodes,
            "frame_count": frames,
            "robot_type": info.get("robot_type"),
            "policy": policy,
            "synthetic": job.get("capture_mode") == "mock"
            or info.get("synthetic") is True,
        }

    # ------------------------------------------------------------- candidates
    def candidates(self) -> list[dict]:
        rows = []
        try:
            self._tool()
            available_reason = ""
        except DomainError as exc:
            available_reason = exc.message
        for job in self.store.all("job"):
            if job["state"] not in DATA_READY_STATES and not job.get("dataset_ready"):
                continue
            row = {
                "id": job["id"],
                "source_job_id": job["id"],
                "dataset_name": job.get("dataset_name")
                or job.get("instruction")
                or job["id"],
                "robot_type": job.get("robot_type")
                or (job.get("profile") or {}).get("robot_type"),
                "state": job["state"],
                "operation": job.get("operation"),
                "created_at": job.get("created_at"),
            }
            try:
                described = self._describe(job)
                if available_reason:
                    raise DomainError(available_reason, 503)
                row.update(
                    described,
                    selectable=True,
                    policy_feature=described["policy"]["feature"],
                )
            except (DomainError, OSError, ValueError, KeyError) as exc:
                row.update(selectable=False, reason=str(exc))
            rows.append(row)
        return rows

    # --------------------------------------------------------------- previews
    def listing(self, job_id: str | None = None) -> list[dict]:
        rows = [
            {
                key: row.get(key)
                for key in (
                    "id",
                    "job_id",
                    "state",
                    "stage",
                    "created_at",
                    "started_at",
                    "finished_at",
                    "error",
                    "summary",
                    "plan_digest",
                )
            }
            for row in self.store.all("trim_preview")
            if job_id is None or row.get("job_id") == job_id
        ]
        rows.sort(key=lambda row: float(row.get("created_at") or 0), reverse=True)
        return rows

    def preview(self, preview_id: str, offset: int = 0, limit: int = 200) -> dict:
        row = self.store.get("trim_preview", preview_id)
        episodes = row.get("episodes") or []
        offset = max(0, int(offset))
        limit = max(1, min(1000, int(limit)))
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "state": row["state"],
            "stage": row.get("stage"),
            "error": row.get("error"),
            "created_at": row.get("created_at"),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "source": row.get("source"),
            "policy": row.get("policy"),
            "summary": row.get("summary"),
            "plan_digest": row.get("plan_digest"),
            "source_snapshot": row.get("source_snapshot"),
            "episodes": episodes[offset : offset + limit],
            "total": len(episodes),
            "offset": offset,
            "limit": limit,
        }

    def enqueue_preview(self, job_id: str, overrides: dict | None = None) -> dict:
        _, python = self._tool()
        overrides = {key: value for key, value in (overrides or {}).items() if value is not None}
        with self.lock:
            job = self.store.get("job", job_id)
            described = self._describe(job)
            snapshot = source_snapshot(Path(described["dataset_path"]))
            policy = self._policy(
                job,
                _read_json(Path(described["dataset_path"]) / "meta/info.json"),
                overrides,
            )
            key = "trim-preview:" + digest(
                {
                    "source_job_id": job_id,
                    "source_snapshot": snapshot,
                    "policy": policy,
                    "algorithm": ALGORITHM,
                }
            )
            for row in self.store.all("trim_preview"):
                if row.get("key") == key and row["state"] in {"QUEUED", "RUNNING", "COMPLETED"}:
                    return row
            row = {
                "id": str(uuid.uuid4()),
                "key": key,
                "job_id": job_id,
                "state": "QUEUED",
                "stage": "等待检测首尾静止帧",
                "created_at": now(),
                "error": None,
                "source": {
                    "job_id": job_id,
                    "dataset_name": job.get("dataset_name") or job_id,
                    "dataset_path": described["dataset_path"],
                    "source_format": described["source_format"],
                    "robot_type": job.get("robot_type"),
                    "episode_count": described["episode_count"],
                    "frame_count": described["frame_count"],
                    "fps": described["fps"],
                },
                "source_snapshot": snapshot,
                "policy": policy,
                "python": python,
            }
            self.store.put("trim_preview", row["id"], row)
            return row

    def tick(self) -> bool:
        queued = [row for row in self.store.all("trim_preview") if row["state"] == "QUEUED"]
        if queued:
            self._run_preview(queued[-1])
            return True
        return False

    def recover(self) -> None:
        for row in self.store.all("trim_preview"):
            if row["state"] == "RUNNING":
                row.update(
                    state="INTERRUPTED",
                    stage="首尾静止帧检测已中断",
                    error="Worker 重启；可重新提交预览",
                    finished_at=now(),
                )
                self.store.put("trim_preview", row["id"], row)

    def _save_preview(self, row, **updates) -> dict:
        row.update(**updates, updated_at=now())
        self.store.put("trim_preview", row["id"], row)
        return row

    def _run_preview(self, row) -> None:
        self._save_preview(row, state="RUNNING", stage="检测首尾静止帧", started_at=now())
        try:
            source = Path(row["source"]["dataset_path"])
            if source_snapshot(source) != row["source_snapshot"]:
                raise DomainError("来源数据集在提交后发生变化，请重新提交预览")
            source_job = self.store.get("job", row["job_id"])
            work = (
                storage_path(self.settings, source_job, "work", source_job["id"])
                / "trim-previews"
                / row["id"]
            )
            work.mkdir(parents=True, exist_ok=False)
            self._save_preview(row, work_path=str(work))
            spec_path = work / "trim-detect-spec.json"
            atomic_json(
                spec_path,
                {
                    "mode": "detect",
                    "source": str(source),
                    "policy": row["policy"],
                },
            )
            code, output, err = self.runner.command(
                [row["python"], str(Path(__file__).with_name("trim_worker.py")), str(spec_path)],
                work,
                "trim-detect",
            )
            if code:
                raise DomainError(
                    "首尾静止帧检测失败："
                    + err.read_text(errors="replace")[-2000:]
                )
            try:
                plan = json.loads(output.read_text())
            except (OSError, ValueError) as exc:
                raise DomainError("裁剪检测未生成有效 JSON") from exc
            if not isinstance(plan, dict) or not plan.get("episodes"):
                raise DomainError("裁剪检测没有返回任何 Episode 结果")
            summary = plan.get("summary") or {}
            if summary.get("episodes") != row["source"]["episode_count"]:
                raise DomainError(
                    "裁剪检测的 Episode 数与来源元数据不一致，请重新提交"
                )
            self._save_preview(
                row,
                state="COMPLETED",
                stage="首尾静止帧检测完成",
                episodes=plan["episodes"],
                summary=summary,
                feature=plan.get("feature"),
                units=plan.get("units"),
                thresholds=plan.get("thresholds"),
                plan_digest=digest(
                    {
                        "algorithm": plan.get("algorithm"),
                        "source_snapshot": row["source_snapshot"],
                        "policy": row["policy"],
                        "episodes": [
                            [item["episode_index"], item["keep_start"], item["keep_stop"], item["status"]]
                            for item in plan["episodes"]
                        ],
                    }
                ),
                finished_at=now(),
            )
        except InterruptedError as exc:
            self._save_preview(
                row,
                state="INTERRUPTED",
                stage="首尾静止帧检测已中断",
                error=str(exc),
                finished_at=now(),
            )
        except DomainError as exc:
            self._save_preview(
                row,
                state="FAILED",
                stage="首尾静止帧检测失败",
                error=exc.message,
                finished_at=now(),
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            self._save_preview(
                row,
                state="FAILED",
                stage="首尾静止帧检测失败",
                error=f"{type(exc).__name__}: {exc}"[:1200],
                finished_at=now(),
            )

    # ------------------------------------------------- conversion integration
    def apply_to_conversion(
        self, job, work: Path, allocation: str | None, info: dict
    ) -> dict:
        """Detect and trim the freshly converted dataset inside ``work``.

        The conversion job asks for this before deep validation. Detection and
        trimming reuse the same worker and policy as the standalone workflow, so
        the two entry points cannot disagree. The trimmed dataset is written to
        ``work/trimmed`` first and only replaces ``work/dataset`` after the plan
        and result counts match, which keeps a failed trim from publishing a
        half-written dataset. Fully static episodes are always kept here: a
        conversion must not silently drop whole episodes.
        """

        _, python = self._tool()
        dataset = work / "dataset"
        policy = self._policy(
            job, info, overrides={"remove_fully_static": False}
        )
        preview_dir = work / "trim-conversion"
        preview_dir.mkdir(parents=True, exist_ok=True)
        detect_spec = preview_dir / "detect-spec.json"
        atomic_json(
            detect_spec,
            {"mode": "detect", "source": str(dataset), "policy": policy},
        )
        self.runner.save(job, stage="检测首尾静止帧")
        code, output, err = self.runner.command(
            [
                python,
                str(Path(__file__).with_name("trim_worker.py")),
                str(detect_spec),
            ],
            work,
            "trim-detect",
            allocation=allocation,
        )
        if code:
            raise DomainError(
                "首尾静止帧检测失败：" + err.read_text(errors="replace")[-2000:]
            )
        try:
            plan = json.loads(output.read_text())
        except (OSError, ValueError) as exc:
            raise DomainError("首尾静止帧检测未生成有效 JSON") from exc
        if not isinstance(plan, dict) or not plan.get("episodes"):
            raise DomainError("首尾静止帧检测没有返回任何 Episode 结果")
        summary = plan.get("summary") or {}
        source_episodes = info.get("total_episodes")
        if summary.get("episodes") != source_episodes:
            raise DomainError("首尾静止帧检测的 Episode 数与转换结果不一致")
        removed_episodes = int(summary.get("episodes_removed") or 0)
        if removed_episodes:
            raise DomainError("转换内的首尾裁剪不允许删除整段 Episode")

        destination = work / "trimmed"
        if destination.exists():
            shutil.rmtree(destination)
        trim_spec = preview_dir / "trim-spec.json"
        atomic_json(
            trim_spec,
            {
                "mode": "trim",
                "source": str(dataset),
                "destination": str(destination),
                "fps": info.get("fps"),
                "policy": policy,
                "episodes": plan["episodes"],
            },
        )
        self.runner.save(job, stage="裁剪首尾静止帧与全部相机视频")
        code, report, err = self.runner.command(
            [python, str(Path(__file__).with_name("trim_worker.py")), str(trim_spec)],
            work,
            "trim",
            allocation=allocation,
        )
        if code:
            raise DomainError(
                "首尾静止帧裁剪失败：" + err.read_text(errors="replace")[-2000:]
            )
        try:
            result = json.loads(report.read_text())
        except (OSError, ValueError) as exc:
            raise DomainError("首尾静止帧裁剪未生成有效 JSON") from exc
        if (
            result.get("validated") is not True
            or result.get("version") != info.get("codebase_version")
            or result.get("episodes") != int(source_episodes or 0) - removed_episodes
            or result.get("frames") != summary.get("frames_kept")
            or result.get("frames_before") != info.get("total_frames")
        ):
            raise DomainError("裁剪结果的 Episode / 帧数与检测计划不一致")

        lineage = {
            "operation": "stationary_trim",
            "entry": "conversion",
            "job_id": job["id"],
            "dataset_name": job.get("dataset_name"),
            "source_format": info.get("codebase_version"),
            "target_format": job.get("target_format"),
            "algorithm": ALGORITHM,
            "policy": policy,
            "summary": summary,
            "mapping": result.get("mapping"),
            "episodes": result.get("episodes_detail"),
            "letools": self.settings.letools_bin,
        }
        atomic_json(destination / "meta/bodypipeline-stationary-trim.json", lineage)
        atomic_json(work / "trim-lineage.json", lineage)
        shutil.rmtree(dataset)
        os.replace(destination, dataset)
        share_data_tree(work)
        return {
            "episodes": int(result["episodes"]),
            "frames": int(result["frames"]),
            "summary": summary,
            "report": result,
            "policy": policy,
        }

    def apply_intervals_to_conversion(
        self, job, work: Path, allocation: str | None, info: dict
    ) -> dict:
        """Detect and remove sustained static intervals inside ``work/dataset``.

        This is the whole-recording counterpart of ``apply_to_conversion``. It
        reuses the same worker: detection returns the kept ranges per source
        episode, the worker writes the same ranges into a new dataset (splitting
        one source episode into several output episodes), and the trimmed tree
        replaces ``work/dataset`` only after deep validation. A fully static
        episode is kept and marked; a conversion never drops whole episodes.
        """

        _, python = self._tool()
        dataset = work / "dataset"
        policy = self._interval_policy(job, info)
        preview_dir = work / "trim-intervals"
        preview_dir.mkdir(parents=True, exist_ok=True)
        detect_spec = preview_dir / "detect-spec.json"
        atomic_json(
            detect_spec,
            {"mode": "detect-static", "source": str(dataset), "policy": policy},
        )
        self.runner.save(job, stage="检测全程静止区间")
        code, output, err = self.runner.command(
            [
                python,
                str(Path(__file__).with_name("trim_worker.py")),
                str(detect_spec),
            ],
            work,
            "trim-detect",
            allocation=allocation,
        )
        if code:
            raise DomainError(
                "全程静止区间检测失败：" + err.read_text(errors="replace")[-2000:]
            )
        try:
            plan = json.loads(output.read_text())
        except (OSError, ValueError) as exc:
            raise DomainError("全程静止区间检测未生成有效 JSON") from exc
        if not isinstance(plan, dict) or not plan.get("episodes"):
            raise DomainError("全程静止区间检测没有返回任何 Episode 结果")
        summary = plan.get("summary") or {}
        source_episodes = info.get("total_episodes")
        if summary.get("episodes") != source_episodes:
            raise DomainError("全程静止区间检测的 Episode 数与转换结果不一致")
        if int(summary.get("episodes_removed") or 0):
            raise DomainError("转换内的全程静止区间裁剪不允许删除整段 Episode")
        invalid = [
            row
            for row in plan["episodes"]
            if row.get("status") in {"invalid", "too_short"}
        ]
        if invalid:
            raise DomainError(
                "全程静止区间检测失败："
                + "；".join(
                    f"Episode {row.get('episode_index')}：{row.get('reason') or row.get('status')}"
                    for row in invalid[:5]
                )
            )

        lineage = {
            "operation": "static_interval_trim",
            "entry": "conversion",
            "job_id": job["id"],
            "dataset_name": job.get("dataset_name"),
            "source_format": info.get("codebase_version"),
            "target_format": job.get("target_format"),
            "algorithm": static_intervals.ALGORITHM,
            "policy": policy,
            "summary": summary,
            "mapping": None,
            "episodes": plan["episodes"],
            "letools": self.settings.letools_bin,
        }
        removed = int(summary.get("frames_removed") or 0)
        if not removed:
            # Nothing to rewrite. Keep the converted dataset untouched and
            # record that detection ran, so a later audit can see the outcome.
            atomic_json(dataset / "meta/bodypipeline-static-intervals.json", lineage)
            atomic_json(work / "trim-intervals-lineage.json", lineage)
            share_data_tree(work)
            return {
                "episodes": int(source_episodes or 0),
                "frames": int(info.get("total_frames") or 0),
                "summary": summary,
                "report": None,
                "policy": policy,
            }

        destination = work / "trimmed"
        if destination.exists():
            shutil.rmtree(destination)
        trim_spec = preview_dir / "trim-spec.json"
        atomic_json(
            trim_spec,
            {
                "mode": "trim",
                "source": str(dataset),
                "destination": str(destination),
                "fps": info.get("fps"),
                "policy": policy,
                "episodes": plan["episodes"],
            },
        )
        self.runner.save(job, stage="删除全程静止区间与全部相机视频")
        code, report, err = self.runner.command(
            [python, str(Path(__file__).with_name("trim_worker.py")), str(trim_spec)],
            work,
            "trim",
            allocation=allocation,
        )
        if code:
            raise DomainError(
                "全程静止区间裁剪失败：" + err.read_text(errors="replace")[-2000:]
            )
        try:
            result = json.loads(report.read_text())
        except (OSError, ValueError) as exc:
            raise DomainError("全程静止区间裁剪未生成有效 JSON") from exc
        if (
            result.get("validated") is not True
            or result.get("version") != info.get("codebase_version")
            or result.get("episodes") != summary.get("output_episodes")
            or result.get("frames") != summary.get("frames_kept")
            or result.get("frames_before") != info.get("total_frames")
        ):
            raise DomainError("全程静止区间裁剪的 Episode / 帧数与检测计划不一致")

        lineage.update(mapping=result.get("mapping"), episodes=result.get("episodes_detail"))
        atomic_json(destination / "meta/bodypipeline-static-intervals.json", lineage)
        atomic_json(work / "trim-intervals-lineage.json", lineage)
        shutil.rmtree(dataset)
        os.replace(destination, dataset)
        share_data_tree(work)
        return {
            "episodes": int(result["episodes"]),
            "frames": int(result["frames"]),
            "summary": summary,
            "report": result,
            "policy": policy,
        }

    # ------------------------------------------------------------------ enqueue
    def enqueue(
        self,
        source_job_id: str,
        preview_id: str,
        name: str | None = None,
        run_doctor: bool = True,
    ) -> dict:
        if type(run_doctor) is not bool:
            raise DomainError("run_doctor 必须为布尔值", 400)
        self._tool()
        with self.lock:
            source_job = self.store.get("job", source_job_id)
            preview = self.store.get("trim_preview", preview_id)
            if preview.get("job_id") != source_job_id:
                raise DomainError("裁剪预览不属于该数据集", 409)
            if preview["state"] != "COMPLETED":
                raise DomainError("请先完成首尾静止帧检测，再创建裁剪任务", 409)
            summary = preview.get("summary") or {}
            kept = int(summary.get("episodes", 0)) - int(summary.get("episodes_removed", 0))
            if kept < 1 or not summary.get("frames_kept"):
                raise DomainError("预览结果没有可保留的帧，无法创建裁剪任务", 409)
            described = self._describe(source_job)
            source_path = Path(described["dataset_path"])
            if str(source_path) != preview["source"]["dataset_path"]:
                raise DomainError("来源数据集路径已变化，请重新提交预览", 409)
            if source_snapshot(source_path) != preview["source_snapshot"]:
                raise DomainError("来源数据集在预览后发生变化，请重新提交预览", 409)
            source_name = (
                source_job.get("dataset_name")
                or source_job.get("instruction")
                or source_job_id
            )
            if isinstance(name, str) and name.strip():
                output_name = dataset_name(name)
            else:
                output_name = dataset_name(f"{str(source_name)[:1980]}{NAME_SUFFIX}")
            key = "stationary-trim:" + digest(
                {
                    "source_job_id": source_job_id,
                    "preview_id": preview_id,
                    "plan_digest": preview["plan_digest"],
                    "name": output_name,
                    "run_doctor": run_doctor,
                    "letools": self.settings.letools_bin,
                }
            )
            for row in self.store.all("job"):
                if row.get("key") == key and row["state"] not in DEDUP_STATES:
                    return row
            job = {
                "id": str(uuid.uuid4()),
                "key": key,
                "operation": "stationary_trim",
                "source_kind": "lerobot_stationary_trim",
                "source_job_id": source_job_id,
                "source_dataset_name": source_name,
                "source_path": str(source_path),
                "source_format": described["source_format"],
                "target_format": described["source_format"],
                "source_snapshot": preview["source_snapshot"],
                "preview_id": preview_id,
                "plan_digest": preview["plan_digest"],
                "trim_policy": preview["policy"],
                "trim_summary": summary,
                "dataset_name": output_name,
                "instruction": "",
                "state": "QUEUED",
                "stage": "等待首尾静止帧裁剪",
                "created_at": now(),
                "error": None,
                "robot_type": source_job.get("robot_type")
                or described.get("robot_type"),
                "capture_mode": "mock"
                if described["synthetic"]
                else "stationary_trim",
                "processing_mode": "real",
                "run_doctor": run_doctor,
                "source_count": described["episode_count"],
                "episode_count": kept,
                "frame_count": int(summary["frames_kept"]),
            }
            for field in ("runtime_id", "provider", "profile", "profile_hash"):
                if source_job.get(field) is not None:
                    job[field] = source_job[field]
            if self.settings.data_root is not None:
                job["data_layout"] = ROBOT_LAYOUT
            destination = storage_path(self.settings, job, "work", job["id"])
            if destination.is_relative_to(source_path):
                raise DomainError("裁剪输出不能位于来源数据集内", 400)
            self.store.put("job", job["id"], job)
            return job

    # ---------------------------------------------------------------------- run
    def run(self, job) -> None:
        save = self.runner.save
        save(job, state="RUNNING", stage="核验来源与裁剪计划", started_at=now())
        try:
            source = Path(job["source_path"])
            with dataset_lock(self.store, self._key(source)):
                preview = self.store.get("trim_preview", job["preview_id"])
                if preview["state"] != "COMPLETED" or preview.get("plan_digest") != job.get("plan_digest"):
                    raise DomainError("裁剪预览已失效，请重新提交预览", 409)
                if str(preview["source"]["dataset_path"]) != str(source):
                    raise DomainError("预览来源与任务来源不一致", 409)
                if source_snapshot(source) != {
                    "signature": job["source_snapshot"].get("signature"),
                    "bytes": job["source_snapshot"].get("bytes"),
                }:
                    raise DomainError("来源数据集在提交后发生变化，请重新提交预览")
                work = data_directory(
                    self.settings, job, "work", job["id"], exist_ok=False
                )
                save(job, work_path=str(work))
                size = int(job["source_snapshot"].get("bytes") or 0)
                if (
                    shutil.disk_usage(work).free
                    < size + size // 4 + self.settings.min_free_bytes
                ):
                    raise DomainError("输出存储空间不足，无法创建裁剪数据集", 507)
                _, python = self._tool()
                spec_path = work / "trim-spec.json"
                atomic_json(
                    spec_path,
                    {
                        "mode": "trim",
                        "source": str(source),
                        "destination": str(work / "dataset"),
                        "fps": preview["source"]["fps"],
                        "policy": job["trim_policy"],
                        "episodes": preview["episodes"],
                    },
                )
                share_data_tree(work)
                save(job, stage="等待裁剪计算资源")
                allocation = self.runner._open_allocation(work)
                save(
                    job,
                    stage=f"裁剪数值数据与 {len(preview.get('episodes') or [])} 段 Episode 的视频",
                )
                code, report, err = self.runner.command(
                    [python, str(Path(__file__).with_name("trim_worker.py")), str(spec_path)],
                    work,
                    "trim",
                    allocation=allocation,
                )
                if code:
                    raise DomainError(
                        "首尾静止帧裁剪失败：" + err.read_text(errors="replace")[-2000:]
                    )
                result = json.loads(report.read_text())
                summary = job.get("trim_summary") or {}
                if (
                    result.get("validated") is not True
                    or result.get("version") != job["source_format"]
                    or result.get("episodes") != job["episode_count"]
                    or result.get("frames") != job["frame_count"]
                    or result.get("frames_before") != summary.get("frames")
                ):
                    raise DomainError("裁剪结果的 Episode / 帧数与预览不一致")
                save(job, stage="深度结构验证")
                tool = executable_path(self.settings.letools_bin)
                dataset = work / "dataset"
                code, _, err = self.runner.command(
                    [tool, "validate", str(dataset), "--deep"],
                    work,
                    "validate",
                    allocation=allocation,
                )
                if code:
                    raise DomainError(
                        "裁剪数据集结构验证失败："
                        + err.read_text(errors="replace")[-2000:]
                    )
                if (
                    source_snapshot(source)["signature"]
                    != job["source_snapshot"].get("signature")
                ):
                    raise DomainError("裁剪期间来源数据集发生变化，结果未发布")
                if self.runner.shutdown_requested.is_set():
                    raise InterruptedError("Worker 收到停止请求")
                lineage = {
                    "operation": "stationary_trim",
                    "job_id": job["id"],
                    "source_job_id": job["source_job_id"],
                    "source_dataset_name": job["source_dataset_name"],
                    "source_path": str(source),
                    "source_format": job["source_format"],
                    "source_snapshot": job["source_snapshot"],
                    "preview_id": job["preview_id"],
                    "plan_digest": job["plan_digest"],
                    "algorithm": ALGORITHM,
                    "policy": job["trim_policy"],
                    "summary": summary,
                    "mapping": result.get("mapping"),
                    "episodes": result.get("episodes_detail"),
                    "letools": self.settings.letools_bin,
                }
                atomic_json(dataset / "meta/bodypipeline-stationary-trim.json", lineage)
                atomic_json(work / "lineage.json", lineage)
                share_data_tree(work)
                info = _read_json(dataset / "meta/info.json")
                if not job.get("run_doctor", True):
                    save(
                        job,
                        state="CONVERTED",
                        stage="首尾静止帧裁剪完成，可预览或运行质检",
                        dataset_ready=True,
                        dataset_path=str(dataset),
                        trim_report=result,
                        finished_at=now(),
                    )
                    return
                save(job, stage="Doctor 全量质检")
                code, report_path, err = self.runner.command(
                    [
                        tool,
                        "doctor",
                        "check",
                        str(dataset),
                        "--checks",
                        ",".join(DOCTOR_CHECK_IDS),
                        "--ci",
                        "--fail-on",
                        "warn",
                    ],
                    work,
                    "doctor",
                    allocation=allocation,
                )
                try:
                    doctor = json.loads(report_path.read_text())
                    if not isinstance(doctor, dict) or not doctor:
                        raise ValueError("empty report")
                except (ValueError, OSError) as exc:
                    raise DomainError(
                        "Doctor 未生成有效 JSON，属于任务错误而不是质量通过："
                        + err.read_text(errors="replace")[-1200:]
                    ) from exc
                gate = doctor_gate(doctor, info, code)
                share_data_tree(work)
                if not gate["passed"]:
                    save(
                        job,
                        state="QUARANTINED",
                        stage="裁剪完成，Doctor 发现问题",
                        error=f"Doctor exit={code}, severity={gate['severity']}",
                        dataset_ready=True,
                        dataset_path=str(dataset),
                        doctor_report=doctor,
                        quality_gate=gate,
                        trim_report=result,
                        finished_at=now(),
                    )
                    return
                save(
                    job,
                    state="SYNTHETIC_READY"
                    if job["capture_mode"] == "mock"
                    else "COMPLETED",
                    stage="首尾静止帧裁剪与 Doctor 质检完成",
                    dataset_ready=True,
                    dataset_path=str(dataset),
                    doctor_report=doctor,
                    quality_gate=gate,
                    trim_report=result,
                    finished_at=now(),
                )
        except InterruptedError as exc:
            save(
                job,
                state="INTERRUPTED",
                stage="首尾静止帧裁剪已中断",
                error=str(exc),
                finished_at=now(),
            )
        except DomainError as exc:
            save(
                job,
                state="FAILED",
                stage="首尾静止帧裁剪失败",
                error=exc.message,
                finished_at=now(),
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            save(
                job,
                state="FAILED",
                stage="首尾静止帧裁剪失败",
                error=f"{type(exc).__name__}: {exc}"[:1200],
                finished_at=now(),
            )
        finally:
            self.runner._close_allocation()


__all__ = ["TrimRunner", "ALGORITHM"]
