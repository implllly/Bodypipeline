"""Queued automatic annotation over registered LeRobot datasets.

Argus ``review`` is a long, model-driven job: it prepares episodes, runs the
deterministic checks, calls the configured model with an optional spend cap and builds
per-episode labels.  This module owns the queue around it: source verification,
a separate worker loop, cancellation, resume, spend accounting and the durable
record the page reads.  It never changes the converted dataset and never touches
the Doctor quality gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from pipeline.shared.core import DomainError, atomic_json, digest, now

from . import argus_annotation
from .datasets import dataset_for_job
from .merges import source_snapshot
from .providers import get_provider
from .providers.base import ANNOTATION_RIGS
from .storage import data_directory, storage_path
from .task_annotations import TemplateLibrary
from .template_annotation import SPEC_NAME, retry_manifest

QUEUED = "QUEUED"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
PARTIAL = "PARTIAL"
FAILED = "FAILED"
INTERRUPTED = "INTERRUPTED"
CANCELLED = "CANCELLED"
ACTIVE = {QUEUED, RUNNING}
FINAL = {COMPLETED, PARTIAL, FAILED, INTERRUPTED, CANCELLED}
REASONING = {"", "none", "minimal", "low", "medium", "high", "xhigh"}
INSPECTION = {"auto", "all", "off"}
# Stages after Argus has started spending on labels. A resume must keep those
# receipts; before them, re-preparing from scratch loses nothing paid for.
LABELLING_STAGES = {"大模型标注", "拼接标注结果", "标注完成", "部分标注完成"}


class BudgetExceeded(DomainError):
    def __init__(self, message: str):
        super().__init__(message, 429)


class AnnotationRunner:
    def __init__(self, settings, store, catalog=None):
        self.settings, self.store, self.catalog = settings, store, catalog
        self.lock = threading.RLock()
        self.shutdown_requested = threading.Event()
        self.cancel_events: dict[str, threading.Event] = {}
        # Per-task model credentials live only here.  They are never written to
        # the annotation record, the work context, logs or exports.
        self.credentials: dict[str, dict] = {}
        self._running = False

    # ------------------------------------------------------------- persistence
    def save(self, row, **fields):
        row.update(**fields, updated_at=now())
        self.store.put("annotation", row["id"], row)
        return row

    def recover(self):
        """A restart cannot adopt a running subprocess; keep finished work."""

        for row in self.store.all("annotation"):
            if row["state"] == RUNNING:
                self.save(
                    row,
                    state=INTERRUPTED,
                    stage="标注已中断",
                    error="Server 重启；已完成的分段和费用保留，可续跑",
                    cancel_requested=False,
                    finished_at=now(),
                )
            elif row["state"] == QUEUED:
                # Model keys only exist in memory, so a queued task cannot run
                # after a restart.  Keep it resumable instead of failing later
                # without an explanation.
                self.save(
                    row,
                    state=INTERRUPTED,
                    stage="标注已中断",
                    error="Server 重启后任务凭据已清除；请续跑并重新填写 baseURL / API Key",
                    cancel_requested=False,
                    finished_at=now(),
                )

    # ---------------------------------------------------------------- settings
    def available(self) -> tuple[bool, str]:
        return argus_annotation.available(self.settings)

    def models(self, *, base_url: str | None = None, api_key: str | None = None):
        return argus_annotation.list_models(self.settings, base_url, api_key)

    def catalog_for(self):
        if self.catalog is None:
            self.catalog = self.settings.runtime_registry()
        return self.catalog

    def options(self) -> dict:
        ok, why = self.available()
        today = self._daily_spend()
        models = list(self.settings.argus_models)
        configured = (self.settings.argus_model or "").strip()
        prices = argus_annotation.price_table(self.settings)

        def priced(name: str) -> bool:
            return str(name or "").split("/")[-1] in prices

        # Never offer a default the allowlist would reject at enqueue time, and
        # prefer one a spend cap can actually bound.
        candidates = [name for name in models if priced(name)] or models
        default_model = (
            configured
            if configured and (not candidates or configured in candidates)
            else (candidates[0] if candidates else configured)
        )
        return {
            "available": ok,
            "template_available": argus_annotation.template_available(self.settings),
            "template_library": TemplateLibrary(self.store).read(),
            "reason": why,
            "models": models,
            "default_model": default_model,
            "priced_models": sorted(prices),
            "prices": prices,
            "default_base_url": (self.settings.argus_base_url or "").strip(),
            "rigs": list(ANNOTATION_RIGS),
            "api_key_configured": argus_annotation.server_key_configured(self.settings),
            "gateway_configured": bool(self.settings.argus_base_url),
            "unlimited": self.settings.argus_annotation_unlimited,
            "defaults": {
                "cap_usd": None
                if self.settings.argus_annotation_unlimited
                else float(self.settings.argus_task_budget_usd),
                "daily_cap_usd": None
                if self.settings.argus_annotation_unlimited
                else float(self.settings.argus_daily_budget_usd),
                "per_episode_budget_usd": None
                if self.settings.argus_annotation_unlimited
                else float(self.settings.argus_per_episode_budget_usd),
                "concurrency": int(self.settings.argus_annotation_concurrency),
                "reasoning": self.settings.argus_annotation_reasoning,
                "sample_fps": float(self.settings.argus_annotation_sample_fps),
                "inspection": self.settings.argus_annotation_inspection,
                "field_dictionary": bool(
                    self.settings.argus_annotation_field_dictionary
                ),
                "max_tokens": int(self.settings.argus_max_tokens),
                "note": self.settings.argus_annotation_note,
            },
            "used_today_usd": round(today, 4),
            "timeout_seconds": int(self.settings.argus_annotation_timeout),
        }

    # ------------------------------------------------------------------ budget
    def _task_spend(self, jid: str) -> float:
        total = 0.0
        for row in self.store.all("annotation"):
            if row.get("job_id") == jid:
                total += float(row.get("cost_usd") or 0.0)
        return total

    def _daily_spend(self) -> float:
        """Spend across content analysis and automatic annotation since midnight."""

        midnight = time.time() - (time.time() % 86400)
        total = 0.0
        for kind in ("analysis", "annotation"):
            for row in self.store.all(kind):
                cost = float(row.get("cost_usd") or 0.0)
                if cost <= 0:
                    continue
                stamp = float(row.get("finished_at") or row.get("created_at") or 0)
                if stamp >= midnight:
                    total += cost
        return total

    def budget(self, jid: str, episodes: int) -> dict:
        if self.settings.argus_annotation_unlimited:
            return {
                "unlimited": True,
                "task_cap_usd": None,
                "task_used_usd": round(self._task_spend(jid), 4),
                "daily_cap_usd": None,
                "daily_used_usd": round(self._daily_spend(), 4),
                "available_usd": None,
            }
        task_cap = float(self.settings.argus_task_budget_usd)
        daily_cap = float(self.settings.argus_daily_budget_usd)
        per_episode = float(self.settings.argus_per_episode_budget_usd)
        batch_cap = per_episode * max(1, int(episodes))
        task_limit = min(task_cap, batch_cap)
        task_used = self._task_spend(jid)
        daily_used = self._daily_spend()
        available = max(0.0, min(task_limit - task_used, daily_cap - daily_used))
        return {
            "task_cap_usd": round(task_limit, 4),
            "task_used_usd": round(task_used, 4),
            "daily_cap_usd": round(daily_cap, 4),
            "daily_used_usd": round(daily_used, 4),
            "available_usd": round(available, 4),
        }

    # --------------------------------------------------------------- candidates
    def candidates(self) -> list[dict]:
        annotations = self.store.all("annotation")
        by_job: dict[str, list[dict]] = {}
        for row in annotations:
            by_job.setdefault(row.get("job_id"), []).append(row)
        rows = []
        for job in self.store.all("job"):
            if job.get("state") in {"DELETING", "DELETE_FAILED", "DELETED"}:
                continue
            try:
                data = dataset_for_job(self.settings, job)
                info = json.loads((data / "meta/info.json").read_text())
            except (DomainError, OSError, ValueError, KeyError):
                continue
            if info.get("codebase_version") not in {"v2.1", "v3.0"}:
                continue
            related = by_job.get(job["id"], [])
            active = next((r for r in related if r["state"] in ACTIVE), None)
            last = related[0] if related else None
            rows.append(
                {
                    "job_id": job["id"],
                    "dataset_name": job.get("dataset_name")
                    or job.get("instruction")
                    or job["id"],
                    "dataset_path": str(data),
                    "robot_type": job.get("robot_type")
                    or (job.get("profile") or {}).get("robot_type"),
                    "runtime_id": job.get("runtime_id"),
                    "provider": job.get("provider"),
                    "target_format": job.get("target_format"),
                    "codebase_version": info.get("codebase_version"),
                    "episode_count": info.get("total_episodes"),
                    "frame_count": info.get("total_frames"),
                    "fps": info.get("fps"),
                    "source_kind": job.get("operation") or "conversion",
                    "annotation_active": active["id"] if active else None,
                    "annotation_last": {
                        "id": last["id"],
                        "state": last["state"],
                        "model": last.get("model"),
                        "cost_usd": last.get("cost_usd"),
                        "created_at": last.get("created_at"),
                    }
                    if last
                    else None,
                }
            )
        return rows

    # ------------------------------------------------------------------ enqueue
    def enqueue(
        self,
        job_id: str,
        *,
        note: str | None = None,
        model: str | None = None,
        cap_usd: float | None = None,
        concurrency: int | None = None,
        reasoning: str | None = None,
        sample_fps: float | None = None,
        inspection: str | None = None,
        field_dictionary: bool | None = None,
        max_tokens: int | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        template_id: str | None = None,
        template_revision: int | None = None,
    ):
        with self.lock:
            job = self.store.get("job", job_id)
            data = dataset_for_job(self.settings, job)
            ok, why = self.available()
            if not ok:
                raise DomainError(f"Argus 自动标注不可用：{why}", 503)

            template = None
            library_revision = None
            if template_id:
                if not argus_annotation.template_available(self.settings):
                    raise DomainError(
                        "Argus 尚未安装模板支持，请重新运行 scripts/install_argus.sh",
                        503,
                    )
                library = TemplateLibrary(self.store).read()
                if (
                    template_revision is not None
                    and template_revision != library["revision"]
                ):
                    raise DomainError("模板库已更新，请刷新模板后再提交 AI 标注", 409)
                template = next(
                    (t for t in library["templates"] if t["id"] == template_id), None
                )
                if template is None:
                    raise DomainError("任务模板不存在，请刷新模板列表", 404)
                if not template["subtasks"] and not template["fields"]:
                    raise DomainError("任务模板至少需要一个 Subtask 或自定义字段", 422)
                library_revision = library["revision"]

            explicit_model = model is not None
            model = (model or self.settings.argus_model or "").strip()
            allowed_models = list(self.settings.argus_models or [])
            # A per-task gateway publishes its own catalogue, so the server
            # allowlist only governs the server's default upstream.
            custom_gateway = bool(str(base_url or "").strip())
            if allowed_models and model not in allowed_models and not custom_gateway:
                if explicit_model:
                    raise DomainError("标注模型不在服务器允许列表中", 400)
                # A configured default outside the allowlist is a server
                # configuration slip; fall back like the page does instead of
                # rejecting every request that omits a model.
                model = allowed_models[0]
            if not model:
                raise DomainError("未配置标注模型", 400)
            gap = argus_annotation.pricing_gap(self.settings, model)
            if gap and not self.settings.argus_annotation_unlimited:
                raise DomainError(gap, 400)
            note = (note or self.settings.argus_annotation_note or "").strip()
            if len(note) > 4000:
                raise DomainError("任务说明不能超过 4000 个字符", 400)
            if reasoning is None:
                reasoning = self.settings.argus_annotation_reasoning
            if reasoning not in REASONING:
                raise DomainError("推理强度无效", 400)
            if inspection is None:
                inspection = self.settings.argus_annotation_inspection
            if inspection not in INSPECTION:
                raise DomainError("证据检查模式无效", 400)
            if concurrency is None:
                concurrency = self.settings.argus_annotation_concurrency
            if not isinstance(concurrency, int) or not 1 <= concurrency <= 64:
                raise DomainError("并发数应在 1 到 64 之间", 400)
            if sample_fps is None:
                sample_fps = self.settings.argus_annotation_sample_fps
            if not isinstance(sample_fps, (int, float)) or not 0 <= sample_fps <= 10:
                raise DomainError("采样帧率应在 0 到 10 之间", 400)
            if field_dictionary is None:
                field_dictionary = self.settings.argus_annotation_field_dictionary
            if type(field_dictionary) is not bool:
                raise DomainError("字段解释选项必须为布尔值", 400)
            if max_tokens is None:
                max_tokens = self.settings.argus_max_tokens
            if not isinstance(max_tokens, int) or not 256 <= max_tokens <= 200000:
                raise DomainError("输出 token 上限应在 256 到 200000 之间", 400)

            for row in self.store.all("annotation"):
                if row["job_id"] == job_id and row["state"] in ACTIVE:
                    if (row.get("task_template") or {}).get("id") != template_id or (
                        template and row.get("template_signature") != digest(template)
                    ):
                        raise DomainError(
                            "该数据集已有使用其他模板的 AI 任务，请等待完成或取消后再提交",
                            409,
                        )
                    return row

            credentials = argus_annotation.resolve_credentials(
                self.settings, base_url, api_key
            )
            self._check_dataset_available(data)
            info = json.loads((data / "meta/info.json").read_text())
            version = info.get("codebase_version")
            if version not in {"v2.1", "v3.0"}:
                raise DomainError(
                    "自动标注仅支持 LeRobot v2.1 / v3.0 数据集；请先完成版本转换",
                    409,
                )
            count = int(info.get("total_episodes") or 1)
            budget = self.budget(job_id, count)
            cap = None
            if not self.settings.argus_annotation_unlimited:
                available = float(budget["available_usd"])
                if available <= 0:
                    raise BudgetExceeded("今日或本数据集的自动标注预算已用尽")
                cap = available if cap_usd is None else float(cap_usd)
                if cap <= 0:
                    raise DomainError("本次费用上限必须大于 0", 400)
                cap = min(cap, available)
            signature = source_snapshot(data)
            context = self._provider_context(job)
            row = {
                "id": str(uuid.uuid4()),
                "job_id": job_id,
                "state": QUEUED,
                "stage": "等待自动标注",
                "created_at": now(),
                "dataset_name": job.get("dataset_name")
                or job.get("instruction")
                or job_id,
                "dataset_path": str(data),
                "robot_type": job.get("robot_type")
                or (job.get("profile") or {}).get("robot_type"),
                "runtime_id": job.get("runtime_id"),
                "provider": job.get("provider"),
                "target_format": job.get("target_format"),
                "data_layout": job.get("data_layout"),
                "codebase_version": info.get("codebase_version"),
                "input_episode_count": count,
                "episode_count": 0,
                "episodes_done": 0,
                "episodes_failed": 0,
                "model": model,
                "task_template": template,
                "template_signature": digest(template) if template else None,
                "template_library_revision": library_revision,
                "note": note,
                "cap_usd": round(cap, 4) if cap is not None else None,
                "concurrency": concurrency,
                "reasoning": reasoning,
                "sample_fps": float(sample_fps),
                "inspection": inspection,
                "field_dictionary": field_dictionary,
                "max_tokens": max_tokens,
                # The gateway URL is not secret and helps the page explain a
                # resumed task.  The key stays in self.credentials.
                "base_url": credentials["task_base_url"],
                "rig": context.get("rig") or "teleop_arms",
                "provider_context": context,
                "source": signature,
                "budget": budget,
                "argus_dir": "argus",
                "attempts": 0,
                "cost_usd": 0.0,
                "carried_cost_usd": 0.0,
                "reserved_cost_usd": 0.0,
                "cost_complete": False,
                "work_path": None,
                "error": None,
                "cancel_requested": False,
                "finished_at": None,
            }
            row["work_path"] = str(
                storage_path(self.settings, row, "annotations", row["id"])
            )
            self.save(row)
            self.credentials[row["id"]] = credentials
            return row

    def _check_dataset_available(self, data: Path):
        key = hashlib.sha256(str(data).encode()).hexdigest()
        try:
            recycle = self.store.get("recycle", key)
        except DomainError as exc:
            if exc.code == 404:
                return
            raise
        if (recycle.get("operation") or {}).get("state") in {"pending", "running"}:
            raise DomainError("数据集正在永久清理 Episode，请等待完成", 409)
        if recycle.get("entries"):
            raise DomainError("数据集存在回收站 Episode，请先还原或永久清理", 409)

    def _provider_context(self, job: dict) -> dict:
        profile = job.get("profile") or {}
        provider_id = job.get("provider")
        runtime = None
        if job.get("runtime_id"):
            try:
                runtime = self.catalog_for().get(job["runtime_id"])
            except (KeyError, AttributeError, DomainError):
                runtime = None
        if runtime is not None:
            provider_id = provider_id or runtime.provider
            if not profile:
                profile = runtime.profile
        if not provider_id:
            return {"rig": "teleop_arms", "cameras": {}}
        try:
            provider = get_provider(provider_id)
        except DomainError:
            # A legacy or imported dataset may name a provider this deployment no
            # longer registers. Without a provider there is no trusted camera or
            # rig fact to add, so the default teleop context is used.
            return {"rig": "teleop_arms", "cameras": {}}
        try:
            context = provider.annotation_context(profile) or {}
            if runtime is not None and runtime.profile is not profile:
                trusted = provider.annotation_context(runtime.profile) or {}
                if not context.get("cameras"):
                    context["cameras"] = trusted.get("cameras") or {}
        except (AttributeError, TypeError):
            # Older third-party providers predate this hook.
            return {"rig": "teleop_arms", "cameras": {}}
        context.setdefault("rig", "teleop_arms")
        return context

    # ------------------------------------------------------------------- state
    def cancel(self, aid: str):
        with self.lock:
            row = self.store.get("annotation", aid)
            if row["state"] not in ACTIVE:
                return row
            event = self.cancel_events.get(aid)
            if event is None:
                event = self.cancel_events[aid] = threading.Event()
            event.set()
            if row["state"] == QUEUED:
                self.cancel_events.pop(aid, None)
                self.credentials.pop(aid, None)
                return self.save(
                    row,
                    state=CANCELLED,
                    stage="已取消",
                    cancel_requested=False,
                    finished_at=now(),
                )
            return self.save(row, stage="正在取消", cancel_requested=True)

    def resume(
        self, aid: str, *, base_url: str | None = None, api_key: str | None = None
    ):
        with self.lock:
            row = self.store.get("annotation", aid)
            if row["state"] in ACTIVE:
                return row
            if row["state"] not in {PARTIAL, FAILED, INTERRUPTED, CANCELLED}:
                raise DomainError("只有失败、部分完成、中断或取消的任务可以续跑", 409)
            ok, why = self.available()
            if not ok:
                raise DomainError(f"Argus 自动标注不可用：{why}", 503)
            if row.get("task_template") and not argus_annotation.template_available(
                self.settings
            ):
                raise DomainError(
                    "Argus 模板支持缺失，请重新运行 scripts/install_argus.sh", 503
                )
            gap = argus_annotation.pricing_gap(self.settings, row.get("model") or "")
            if gap and not self.settings.argus_annotation_unlimited:
                raise DomainError(gap, 400)
            job = self.store.get("job", row["job_id"])
            data = dataset_for_job(self.settings, job)
            if str(data) != row.get("dataset_path"):
                raise DomainError("来源数据集路径已变化，请创建新的标注任务", 409)
            current = source_snapshot(data)
            saved = row.get("source") or {}
            if (
                saved.get("signature")
                and current.get("signature") != saved["signature"]
            ):
                raise DomainError(
                    "来源数据集内容已变化，不能续跑；请创建新的标注任务", 409
                )
            # A server restart clears the in-memory key, so resuming asks for
            # credentials again unless the server has its own default.
            credentials = argus_annotation.resolve_credentials(
                self.settings,
                base_url if base_url is not None else row.get("base_url"),
                api_key,
            )
            fields = {
                "state": QUEUED,
                "stage": "等待续跑",
                "error": None,
                "finished_at": None,
                "cancel_requested": False,
                "base_url": credentials["task_base_url"],
            }
            budget = self.budget(
                row["job_id"], int(row.get("input_episode_count") or 1)
            )
            if budget["available_usd"] is not None and budget["available_usd"] <= 0:
                raise BudgetExceeded("今日或本数据集的自动标注预算已用尽，不能续跑")
            fields["budget"] = budget
            previous = float(row.get("cost_usd") or 0.0)
            fields["cap_usd"] = (
                None
                if self.settings.argus_annotation_unlimited
                else round(
                    min(
                        float(row.get("cap_usd") or budget["available_usd"]),
                        previous + float(budget["available_usd"]),
                    ),
                    4,
                )
            )
            if self._preparation_refused(row.get("error") or ""):
                fields["fresh_start"] = True
            elif not row.get("episodes_done") and not float(row.get("cost_usd") or 0):
                # A run killed before it paid for a label may leave a half-written
                # preparation that Argus refuses to resume. Nothing paid is lost by
                # starting over, so do that on the first retry instead of failing
                # once more and asking the operator to resume twice.
                fields["fresh_start"] = (
                    str(row.get("stage") or "") not in LABELLING_STAGES
                )
            self.save(row, **fields)
            self.credentials[aid] = credentials
            return row

    @staticmethod
    def _preparation_refused(error: str) -> bool:
        text = str(error).lower()
        return (
            "preparation cannot be verified" in text
            or "source changed during review" in text
            or "input resource changed" in text
        )

    @staticmethod
    def _scrub_log(path: Path, secret: str):
        """Remove a task key from one log file once the subprocess is done."""

        if not secret:
            return
        temp = path.with_name(path.name + ".scrub")
        try:
            if not path.is_file():
                return
            needle = secret.encode()
            keep = max(0, len(needle) - 1)
            changed = False
            with path.open("rb") as source, temp.open("wb") as target:
                carry = b""
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    data = carry + chunk
                    if needle in data:
                        changed = True
                        data = data.replace(needle, b"[redacted]")
                    carry = data[-keep:] if keep else b""
                    target.write(data[:-keep] if keep else data)
                target.write(carry)
            if changed:
                temp.replace(path)
            else:
                temp.unlink()
        except OSError:
            temp.unlink(missing_ok=True)

    def _scrub_logs(self, work: Path, log: Path, secret: str):
        self._scrub_log(log, secret)
        logs = work / "argus/logs"
        if not logs.is_dir():
            return
        for path in sorted(logs.glob("*.log")):
            self._scrub_log(path, secret)

    # --------------------------------------------------------------------- run
    def tick(self):
        if self.shutdown_requested.is_set() or self._running:
            return False
        queued = [r for r in self.store.all("annotation") if r["state"] == QUEUED]
        if not queued:
            return False
        self.run(queued[-1])
        return True

    def run(self, row):
        with self.lock:
            if row["state"] != QUEUED:
                return row
            event = threading.Event()
            self.cancel_events[row["id"]] = event
            self._running = True
            row = self.save(
                row,
                state=RUNNING,
                stage="准备自动标注",
                started_at=now(),
                finished_at=None,
                error=None,
                attempts=int(row.get("attempts") or 0) + 1,
                cancel_requested=False,
            )
        work = storage_path(self.settings, row, "annotations", row["id"])
        log = work / "logs/run.log"
        state = RUNNING
        error = None
        exit_code = None
        exit_note = None
        started = time.monotonic()
        process = None
        secret = ""
        try:
            work = data_directory(self.settings, row, "annotations", row["id"])
            credentials = self.credentials.get(row["id"])
            if credentials is None:
                # Direct runs and old queued rows fall back to server defaults;
                # a task key is never written down, so this path cannot restore
                # one after a restart.
                credentials = argus_annotation.resolve_credentials(
                    self.settings, row.get("base_url"), None
                )
            secret = str(credentials.get("api_key") or "")
            log.parent.mkdir(parents=True, exist_ok=True)
            if row.get("fresh_start"):
                # Argus refused a previous preparation whose products cannot be
                # trusted. Keep them for diagnosis, but never let review read them.
                previous = work / "argus"
                if previous.exists():
                    # Name the archive after the attempt that produced it.
                    attempt = max(1, int(row.get("attempts") or 2) - 1)
                    archive = work / f"argus-attempt-{attempt}"
                    counter = 1
                    while archive.exists():
                        archive = work / f"argus-attempt-{attempt}-{counter}"
                        counter += 1
                    previous.rename(archive)
                # The archived attempt may already have paid for labels. Its
                # sidecars stop being read, so carry the settled amount forward
                # instead of losing it from the task and daily ledgers.
                carried = round(
                    float(row.get("carried_cost_usd") or 0.0)
                    + float(row.get("cost_usd") or 0.0),
                    6,
                )
                row = self.save(
                    row,
                    fresh_start=False,
                    carried_cost_usd=carried,
                    cost_usd=carried,
                    episode_count=0,
                    episodes_done=0,
                    episodes_failed=0,
                    units=0,
                )
            data = Path(row["dataset_path"])
            current = source_snapshot(data)
            if (row.get("source") or {}).get("signature") not in {
                None,
                current.get("signature"),
            }:
                raise DomainError("来源数据集内容已变化，已停止标注")
            (work / "context.json").write_text(
                json.dumps(
                    {
                        "annotation_id": row["id"],
                        "job_id": row["job_id"],
                        "dataset_name": row["dataset_name"],
                        "dataset_path": str(data),
                        "source": row.get("source"),
                        "robot_type": row.get("robot_type"),
                        "runtime_id": row.get("runtime_id"),
                        "provider": row.get("provider"),
                        "rig": row.get("rig"),
                        "provider_context": row.get("provider_context"),
                        "model": row["model"],
                        "note": row.get("note"),
                        "cap_usd": row.get("cap_usd"),
                        "parameters": {
                            "task_template": row.get("task_template"),
                            "template_signature": row.get("template_signature"),
                            "template_library_revision": row.get(
                                "template_library_revision"
                            ),
                            "concurrency": row.get("concurrency"),
                            "reasoning": row.get("reasoning"),
                            "sample_fps": row.get("sample_fps"),
                            "inspection": row.get("inspection"),
                            "field_dictionary": row.get("field_dictionary"),
                            "max_tokens": row.get("max_tokens"),
                        },
                        "argus": {
                            "src": self.settings.argus_src,
                            "python": self.settings.argus_python,
                            "model": row["model"],
                            "base_url": credentials.get("base_url") or "",
                        },
                        "created_at": row["created_at"],
                        "attempt": row.get("attempts"),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            if row.get("task_template"):
                if digest(row["task_template"]) != row.get("template_signature"):
                    raise DomainError("AI 任务的模板快照已变化，不能继续标注", 409)
                spec = {
                    "version": 1,
                    "template": row["task_template"],
                    "signature": row["template_signature"],
                }
                spec_path = work / SPEC_NAME
                if spec_path.exists() and json.loads(spec_path.read_text()) != spec:
                    raise DomainError("AI 任务模板文件与快照不一致，不能继续标注", 409)
                atomic_json(spec_path, spec)
                if int(row.get("attempts") or 0) > 1:
                    atomic_json(
                        work / "task-template-invalid.json", retry_manifest(work)
                    )
            argv = argus_annotation.command(self.settings, row, data, work)
            env = argus_annotation.environment(
                self.settings,
                row,
                row.get("provider_context") or {},
                credentials,
            )
            with log.open("ab") as out:
                out.write(
                    (
                        f"\n=== annotation attempt {row.get('attempts')} "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    ).encode()
                )
                out.flush()
                process = subprocess.Popen(
                    argv,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    cwd=argus_annotation.source_path(self.settings),
                    env=env,
                )
                deadline = started + float(self.settings.argus_annotation_timeout)
                offset = 0
                stage = row["stage"]
                next_update = 0.0
                while process.poll() is None:
                    if event.is_set():
                        state, stage, exit_note = CANCELLED, "已取消", "已取消"
                        _terminate(process)
                        break
                    if self.shutdown_requested.is_set():
                        state, stage, exit_note = (
                            INTERRUPTED,
                            "已中断",
                            "Server 正在停止",
                        )
                        _terminate(process)
                        break
                    if time.monotonic() >= deadline:
                        state, exit_note = FAILED, "自动标注执行超时"
                        _terminate(process)
                        break
                    chunk, offset = _tail(log, offset)
                    if chunk:
                        stage = argus_annotation.stage_from_log(chunk, stage)
                    if time.monotonic() >= next_update:
                        next_update = time.monotonic() + 2.0
                        progress = argus_annotation.progress(work)
                        # Re-read first: a concurrent cancel must not be undone by
                        # writing back a stale copy of the record.
                        row = self.save(
                            self.store.get("annotation", row["id"]),
                            stage=stage,
                            episode_count=progress["episodes"],
                            episodes_done=progress["labelled"],
                            units=progress["units"],
                        )
                    time.sleep(0.5)
            exit_code = process.returncode
            # A gateway error can echo a request header.  Scrub the key before
            # the failure tail is stored or any log is served.
            self._scrub_logs(work, log, secret)
            chunk, _ = _tail(log, offset)
            if chunk:
                stage = argus_annotation.stage_from_log(chunk, stage)

            result = argus_annotation.cost(work)
            episodes = argus_annotation.outputs(work)
            labelled = sum(1 for item in episodes if item["labels_available"])
            failed = len(episodes) - labelled
            run_status = result.get("status")
            carried = float(row.get("carried_cost_usd") or 0.0)
            run_cost = _number(result.get("cost_usd")) or 0.0
            if state == RUNNING:
                if labelled and exit_code == 0 and run_status == "done" and not failed:
                    state = COMPLETED
                    stage = "标注完成"
                elif labelled:
                    state = PARTIAL
                    stage = "部分标注完成"
                    error = _failure_error(log, episodes)
                else:
                    state = FAILED
                    stage = "标注失败"
                    error = _failure_error(log, episodes)
            elif state == FAILED and labelled:
                # A timeout still leaves the episodes labelled before it usable.
                state = PARTIAL
                stage = "部分标注完成"
                error = _failure_error(log, episodes)
            self.save(
                row,
                state=state,
                stage=stage,
                error=error,
                exit_code=exit_code,
                exit_note=exit_note,
                finished_at=now(),
                cost_usd=round(run_cost + carried, 6),
                reserved_cost_usd=_number(result.get("reserved_cost_usd")) or 0.0,
                cost_complete=bool(result.get("cost_complete")),
                episode_count=len(episodes),
                episodes_done=labelled,
                episodes_failed=failed,
                work_path=str(work),
                argus_path=str(work / "argus"),
                cancel_requested=False,
            )
        except DomainError as exc:
            self.save(
                row,
                state=FAILED,
                stage="标注失败",
                error=exc.message,
                finished_at=now(),
                cancel_requested=False,
                work_path=str(work),
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            if process is not None and process.poll() is None:
                _terminate(process)
            self.save(
                row,
                state=FAILED,
                stage="标注失败",
                error=f"{type(exc).__name__}: {exc}"[:1200],
                finished_at=now(),
                cancel_requested=False,
                work_path=str(work),
            )
        finally:
            self._scrub_logs(work, log, secret)
            self.credentials.pop(row["id"], None)
            self.cancel_events.pop(row["id"], None)
            with self.lock:
                self._running = False
        return self.store.get("annotation", row["id"])

    # ---------------------------------------------------------------- results
    def work(self, row) -> Path:
        recorded = row.get("work_path")
        if recorded:
            return Path(recorded)
        return storage_path(self.settings, row, "annotations", row["id"])

    def episodes(self, aid: str) -> list[dict]:
        row = self.store.get("annotation", aid)
        work = self.work(row)
        if not work.is_dir():
            return []
        return argus_annotation.outputs(work)

    def episode(self, aid: str, episode_id: str) -> dict:
        row = self.store.get("annotation", aid)
        result = argus_annotation.episode(self.work(row), episode_id)
        if result is None:
            raise DomainError("标注 Episode 不存在", 404)
        return result

    def clips(self, aid: str, episode_id: str) -> list[dict]:
        row = self.store.get("annotation", aid)
        return argus_annotation.clips(self.work(row), episode_id)

    def clip(self, aid: str, episode_id: str, camera: str) -> Path | None:
        row = self.store.get("annotation", aid)
        return argus_annotation.clip(self.work(row), episode_id, camera)

    def log_names(self, aid: str) -> list[str]:
        row = self.store.get("annotation", aid)
        return argus_annotation.log_names(self.work(row))

    def log(self, aid: str, name: str) -> str:
        row = self.store.get("annotation", aid)
        text = argus_annotation.read_log(self.work(row), name)
        secret = str((self.credentials.get(aid) or {}).get("api_key") or "")
        return argus_annotation.redact(text, secret)

    def export(self, aid: str) -> dict:
        row = self.store.get("annotation", aid)
        work = self.work(row)
        episodes = argus_annotation.outputs(work)
        details = []
        for item in episodes:
            value = argus_annotation.episode(work, item["id"])
            if value is not None:
                details.append(value)
        return {
            "annotation": {
                key: row.get(key)
                for key in (
                    "id",
                    "job_id",
                    "dataset_name",
                    "dataset_path",
                    "base_url",
                    "robot_type",
                    "model",
                    "task_template",
                    "template_signature",
                    "template_library_revision",
                    "note",
                    "cap_usd",
                    "concurrency",
                    "reasoning",
                    "sample_fps",
                    "inspection",
                    "field_dictionary",
                    "max_tokens",
                    "state",
                    "stage",
                    "created_at",
                    "started_at",
                    "finished_at",
                    "episodes_done",
                    "episodes_failed",
                    "cost_usd",
                    "carried_cost_usd",
                    "reserved_cost_usd",
                    "cost_complete",
                    "source",
                )
            },
            "argus": argus_annotation.cost(work),
            "report": argus_annotation.read_report(work),
            "episodes": details,
        }


def _tail(path: Path, offset: int) -> tuple[str, int]:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
            return chunk.decode(errors="replace"), handle.tell()
    except OSError:
        return "", offset


def _terminate(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait()


def _failure_tail(log: Path) -> str:
    try:
        size = log.stat().st_size
        with log.open("rb") as handle:
            handle.seek(max(0, size - 16000))
            text = handle.read().decode(errors="replace")
    except OSError:
        return "Argus 未生成可用标注"
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return "\n".join(lines[-8:])[-1600:] or "Argus 未生成可用标注"


def _failure_error(log: Path, episodes: list[dict]) -> str:
    """The run tail plus why each failed episode produced nothing.

    Argus writes the useful reason (an unreachable gateway, a rejected key, a
    truncated reply) to its own per-stage log and the episode sidecar, while the
    Server-visible tail only says that labelling failed.  Put the reason back
    where the task list can show it.
    """

    text = _failure_tail(log)
    reasons: list[str] = []
    for item in episodes:
        failure = item.get("label_failure")
        why = ""
        if isinstance(failure, dict):
            why = str(failure.get("why") or "").strip()
        if not why:
            continue
        line = f"{item.get('episode_id') or item.get('id')}: {why}"
        if line not in reasons:
            reasons.append(line)
    if reasons:
        text = (text + "\n" + "\n".join(reasons[:3]))[:1600]
    return text


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


__all__ = [
    "ACTIVE",
    "AnnotationRunner",
    "BudgetExceeded",
    "CANCELLED",
    "COMPLETED",
    "FAILED",
    "FINAL",
    "INTERRUPTED",
    "PARTIAL",
    "QUEUED",
    "RUNNING",
]
