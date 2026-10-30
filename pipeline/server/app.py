from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import quote

from fastapi import Body, FastAPI, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from pipeline.shared.collections import collection_metadata, group_uploads
from pipeline.shared.core import DomainError, ProcessLock, executable_path
from pipeline.shared.security import install_security
from pipeline.shared.web import mount_ui

from .analyses import AnalysisRunner
from .annotations import AnnotationRunner
from .checks import QualityRunner
from .conversion_options import ConversionOptions
from .dataset_management import DELETION_STATES, DatasetManager
from .datasets import TARGET_FORMATS, dataset_for_job
from .episode_validation import InvalidEpisodePolicy
from .imports import ImportService
from .ingest import IngestService
from .jobs import JobRunner
from .providers import RuntimeCatalog
from .quality import robot_profile_blockers
from .storage import storage_path
from .subtask_review import DraftRequest, RevisionExportRequest, SubtaskReview
from .task_annotations import TemplateLibrary, TemplateRequest
from .viewer import ViewerManager


class ConversionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_format: Literal["v3.0", "v2.1"] = "v3.0"
    run_doctor: StrictBool = True
    instruction: str | None = Field(default=None, min_length=1, max_length=2000)
    conversion_options: ConversionOptions = Field(default_factory=ConversionOptions)
    invalid_episode_policy: InvalidEpisodePolicy = "fail"


class BatchConversionRequest(ConversionRequest):
    instruction: str = Field(min_length=1, max_length=2000)
    upload_ids: list[str] | None = Field(default=None, min_length=1, max_length=1000)


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    robot_type: str | None = Field(default=None, min_length=1, max_length=64)
    runtime_id: str | None = Field(default=None, min_length=1, max_length=128)


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["local", "full"] = "local"
    episodes: list[int] | None = Field(default=None, max_length=1000)


class AnnotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=4000)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    cap_usd: float | None = Field(default=None, gt=0, le=1_000_000)
    concurrency: int | None = Field(default=None, ge=1, le=64)
    reasoning: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None
    ) = None
    sample_fps: float | None = Field(default=None, gt=0, le=10)
    inspection: Literal["auto", "all", "off"] | None = None
    field_dictionary: StrictBool | None = None
    max_tokens: int | None = Field(default=None, ge=256, le=200000)
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, max_length=4096)


class AnnotationResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, max_length=4096)


class AnnotationModelsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: str | None = Field(default=None, max_length=4096)


class MergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_job_ids: list[str] = Field(min_length=2, max_length=100)
    name: str = Field(min_length=1, max_length=2000)


class VersionConversionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_job_id: str = Field(min_length=1, max_length=128)
    target_format: Literal["v3.0", "v2.1"]
    name: str | None = Field(default=None, min_length=1, max_length=2000)
    run_doctor: StrictBool = True


class SubtaskExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=2000)


class DatasetDeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["raw", "result"]
    id: str = Field(min_length=1, max_length=128)
    confirmation: str = Field(min_length=1, max_length=2000)


log = logging.getLogger("pipeline.server")


def create_app(settings, background=True):
    ingest = IngestService(settings)
    runtimes = RuntimeCatalog(settings)
    imports = ImportService(settings, ingest, runtimes)
    runner = JobRunner(settings, ingest)
    subtask_review = SubtaskReview(settings, ingest.store)
    checks = QualityRunner(
        settings,
        ingest.store,
        runner.command,
        open_allocation=runner._open_allocation,
        close_allocation=runner._close_allocation,
    )
    analyses = AnalysisRunner(settings, ingest.store, runner.command)
    annotations = AnnotationRunner(settings, ingest.store, runtimes)
    viewer = ViewerManager(settings, ingest.store)
    datasets = DatasetManager(settings, runner, viewer)
    # Enqueueing consumers and marking deletions share one admission lock.
    checks.lock = analyses.lock = annotations.lock = runner.lock
    lock = ProcessLock(settings.root / "server.lock")
    stopping = asyncio.Event()

    async def loop():
        while not stopping.is_set():
            # One failing worker must not stop the shared loop: a dead loop means
            # every later job, check and analysis silently stops being picked up.
            for worker in (datasets.tick, runner.tick, checks.tick, analyses.tick):
                if stopping.is_set():
                    break
                try:
                    await asyncio.to_thread(worker)
                except Exception:
                    log.exception(
                        "worker tick failed: %s", getattr(worker, "__self__", worker)
                    )
            try:
                await asyncio.wait_for(stopping.wait(), settings.poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def viewer_loop():
        # The Visualizer is a long-lived service: start it with the Server so a
        # preview click only has to register a dataset.
        await asyncio.to_thread(viewer.autostart)
        while not stopping.is_set():
            await asyncio.to_thread(viewer.poll)
            try:
                await asyncio.wait_for(stopping.wait(), 2)
            except asyncio.TimeoutError:
                pass

    async def annotation_loop():
        # Automatic annotation is a long model-driven run. It gets its own
        # queue so a slow review never delays conversions, checks or analyses.
        while not stopping.is_set():
            try:
                await asyncio.to_thread(annotations.tick)
            except Exception:
                log.exception("annotation worker tick failed")
            try:
                await asyncio.wait_for(stopping.wait(), settings.poll_seconds)
            except asyncio.TimeoutError:
                pass

    @asynccontextmanager
    async def lifespan(app):
        lock.acquire()
        ingest.reconcile()
        runner.recover()
        checks.recover()
        analyses.recover()
        annotations.recover()
        viewer.recover()
        datasets.recover()
        tasks = (
            [
                asyncio.create_task(loop()),
                asyncio.create_task(annotation_loop()),
                asyncio.create_task(viewer_loop()),
            ]
            if background
            else []
        )
        try:
            yield
        finally:
            stopping.set()
            runner.shutdown_requested.set()
            annotations.shutdown_requested.set()
            if tasks:
                await asyncio.gather(*tasks)
            viewer.stop()
            lock.release()

    app = FastAPI(title="Embodied Pipeline Server", lifespan=lifespan)
    app.state.ingest, app.state.runner, app.state.viewer = ingest, runner, viewer
    app.state.imports = imports
    app.state.checks = checks
    app.state.analyses = analyses
    app.state.annotations = annotations
    app.state.runtimes = runtimes
    app.state.datasets = datasets
    install_security(app, settings, device_prefix="/device/")
    mount_ui(app, report=True)

    @app.get("/healthz")
    def live():
        return {"service": "server", "alive": True}

    @app.get("/api/config")
    def config():
        return {
            "role": "server",
            "processing_mode": settings.processing_mode,
            "auto_process": settings.auto_process,
            "robot_id": settings.robot_id,
            "profile": settings.profile(),
            "default_runtime_id": settings.default_profile or runtimes.default().id,
            "runtimes": runtimes.public(),
            "conversion_formats": [
                {"id": f, "label": "LeRobot " + f} for f in TARGET_FORMATS
            ],
            "default_target_format": "v3.0",
            "letools_available": bool(executable_path(settings.letools_bin)),
            "viewer_url": settings.viewer_origin.rstrip("/") + "/",
            "import_roots": [str(root) for root in imports.allowed_roots()],
        }

    @app.post("/api/imports", status_code=201)
    def import_dataset(body: ImportRequest):
        with runner.lock, ingest.lock:
            datasets.check_import(body.path)
            return imports.import_path(
                body.path, body.robot_type, runtime_id=body.runtime_id
            )

    @app.get("/api/datasets")
    def managed_datasets():
        # Listing is advisory; enqueue revalidates under both admission locks.
        # Filesystem reads must not hold up uploads or new conversion requests.
        return datasets.listing()

    @app.post("/api/dataset-deletions", status_code=202)
    def delete_dataset(body: DatasetDeletionRequest):
        return datasets.enqueue(body.kind, body.id, body.confirmation)

    @app.post("/api/dataset-deletions/{identifier}/retry", status_code=202)
    def retry_deletion(identifier: str):
        return datasets.retry(identifier)

    def authorize_upload(uid, request):
        row = ingest.store.get("upload", uid)
        device = request.state.device
        if (
            row["robot_id"] != device["robot_id"]
            or row.get("project_id", settings.project_id) != device["project_id"]
        ):
            raise DomainError("该上传不属于当前设备", 403)

    @app.get("/device/targets")
    def targets(request: Request):
        allowed = request.state.device.get("targets", list(settings.targets))
        return {
            "targets": [
                {"id": k, "label": v}
                for k, v in settings.targets.items()
                if k in allowed
            ]
        }

    @app.post("/device/uploads", status_code=201)
    async def upload(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > settings.max_manifest_bytes:
                raise DomainError("Manifest 超过上限", 413)
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError()
            manifest = data["manifest"]
            if not isinstance(manifest, dict):
                raise ValueError()
            runtime = runtimes.for_device(
                request.state.device, raw_format=manifest.get("raw_format")
            )
            return await asyncio.to_thread(
                ingest.create,
                manifest,
                data["target"],
                data.get("prefix", ""),
                request.state.device,
                runtime,
            )
        except (ValueError, KeyError, TypeError):
            raise DomainError("无效上传请求", 400)

    @app.get("/device/uploads/{uid}")
    def status(uid: str, request: Request):
        authorize_upload(uid, request)
        return ingest.offsets(uid)

    @app.put("/device/uploads/{uid}/files/{index}")
    async def chunk(uid: str, index: int, request: Request, offset: int):
        authorize_upload(uid, request)
        content = bytearray()
        async for part in request.stream():
            content.extend(part)
            if len(content) > settings.chunk_bytes:
                raise DomainError("分块超限", 413)
        return await asyncio.to_thread(
            ingest.chunk,
            uid,
            index,
            offset,
            request.headers.get("x-chunk-sha256", ""),
            bytes(content),
        )

    @app.post("/device/uploads/{uid}/files/{index}/reset")
    def reset(uid: str, index: int, request: Request):
        authorize_upload(uid, request)
        return ingest.reset_file(uid, index)

    @app.post("/device/uploads/{uid}/complete")
    def complete(uid: str, request: Request):
        authorize_upload(uid, request)
        return ingest.complete(uid)

    @app.get("/api/uploads")
    def uploads():
        rows = []
        for row in ingest.store.all("upload"):
            if row["state"] in DELETION_STATES:
                continue
            result = {**row, "conversion_blockers": []}
            blockers = result["conversion_blockers"]
            try:
                manifest = ingest.manifest(row)
                result.update(
                    collection_metadata(manifest, row["target"], row["prefix"])
                )
                source_type = manifest["profile"].get("robot_type")
                result["robot_type"] = row.get("robot_type") or source_type
                try:
                    runtime = runtimes.for_stored_record(row, manifest)
                    provider = runtimes.provider(runtime)
                except DomainError:
                    # Legacy visibility: keep unknown robot types visible with
                    # actionable blockers instead of hiding their manifests.
                    blockers.extend(
                        [
                            {
                                "field": "robot_type",
                                "message": "当前服务器转换配置不适用于此机器人类型；需部署对应 Provider 和 Runtime",
                            },
                            {
                                "field": "profile_hash",
                                "message": "没有可用于此采集的已批准 Runtime/Profile",
                            },
                        ]
                    )
                    rows.append(result)
                    continue
                result["runtime_id"] = runtime.id
                result["provider"] = runtime.provider
                result["robot_type"] = runtime.robot_type
                if source_type != runtime.robot_type:
                    blockers.append(
                        {
                            "field": "robot_type",
                            "message": "采集声明的 robot_type 与设备绑定的 Runtime 不一致",
                        }
                    )
                if manifest.get("raw_format") not in runtime.raw_formats:
                    blockers.append(
                        {
                            "field": "raw_format",
                            "message": "采集原始格式不属于该 Runtime 的允许列表",
                        }
                    )
                if not runtime.accepts_profile_hash(manifest["profile_hash"]):
                    blockers.append(
                        {
                            "field": "profile_hash",
                            "message": "采集 Profile 与 Runtime 批准版本不同；需核实后加入兼容摘要",
                        }
                    )
                if manifest["capture_mode"] != "mock":
                    blockers.extend(provider.profile_blockers(runtime.profile))
            except (DomainError, OSError, ValueError, KeyError) as exc:
                blockers.append(
                    {
                        "field": "manifest",
                        "message": "采集清单不可读取或校验失败：" + str(exc),
                    }
                )
            rows.append(result)
        return rows

    @app.get("/api/collections")
    def collections():
        return group_uploads(uploads())

    @app.post("/api/collections/{cid}/process", status_code=202)
    def process_collection(cid: str, body: BatchConversionRequest):
        return job_response(runner.enqueue_collection(cid, **body.model_dump()))

    def job_response(row):
        result = {**row, "target_format": row.get("target_format", "v3.0")}
        result["robot_type"] = row.get("robot_type") or row.get("profile", {}).get(
            "robot_type"
        )
        if row["state"] == "BLOCKED":
            blockers = row.get("blockers")
            if blockers is None:
                try:
                    runtime = runner.runtime_for_job(row)
                    blockers = runner.catalog.provider(runtime).profile_blockers(
                        row.get("profile", runtime.profile)
                    )
                except (DomainError, ValueError, KeyError):
                    blockers = robot_profile_blockers(row.get("profile", {}))
            result["blockers"] = blockers
        try:
            result["dataset_path"] = str(dataset_for_job(settings, row))
            result["dataset_available"] = True
        except DomainError as exc:
            result["dataset_available"] = False
            result["dataset_unavailable_reason"] = str(exc)
        return result

    @app.get("/api/jobs")
    def jobs():
        return [job_response(row) for row in ingest.store.all("job") if row["state"] not in DELETION_STATES]

    @app.get("/api/merge-candidates")
    def merge_candidates():
        return runner.merges.candidates()

    @app.get("/api/version-conversion-candidates")
    def version_conversion_candidates():
        return runner.version_conversions.candidates()

    @app.post("/api/version-conversions", status_code=202)
    def convert_version(body: VersionConversionRequest):
        return job_response(
            runner.version_conversions.enqueue(
                body.source_job_id,
                body.target_format,
                name=body.name,
                run_doctor=body.run_doctor,
            )
        )

    @app.post("/api/merges", status_code=202)
    def merge(body: MergeRequest):
        return job_response(runner.merges.enqueue(body.source_job_ids, body.name))

    @app.post("/api/uploads/{uid}/process", status_code=202)
    def process(uid: str, body: ConversionRequest | None = Body(default=None)):
        options = body or ConversionRequest()
        return job_response(runner.enqueue(uid, **options.model_dump()))

    @app.get("/api/jobs/{jid}")
    def job(jid: str):
        return job_response(ingest.store.get("job", jid))

    @app.post("/api/jobs/{jid}/checks", status_code=202)
    def run_check(jid: str):
        # Doctor and the content analysis answer different questions about the
        # same dataset, so starting one starts both. Argus does not need letools,
        # so it is enqueued first: a broken Doctor install must not hide it.
        if settings.argus_src:
            try:
                analyses.enqueue(jid, mode="local")
            except DomainError:
                pass
        return checks.enqueue(jid)

    @app.get("/api/jobs/{jid}/checks")
    def job_checks(jid: str):
        ingest.store.get("job", jid)
        return [r for r in ingest.store.all("check") if r["job_id"] == jid]

    @app.post("/api/jobs/{jid}/analyses", status_code=202)
    def run_analysis(jid: str, body: AnalysisRequest | None = Body(default=None)):
        options = body or AnalysisRequest()
        return analyses.enqueue(jid, mode=options.mode, episodes=options.episodes)

    @app.get("/api/jobs/{jid}/analyses")
    def job_analyses(jid: str):
        ingest.store.get("job", jid)
        return [r for r in ingest.store.all("analysis") if r["job_id"] == jid]

    @app.get("/api/analyses/{aid}")
    def analysis(aid: str):
        return ingest.store.get("analysis", aid)

    @app.post("/api/analyses/{aid}/cancel")
    def cancel_analysis(aid: str):
        return analyses.cancel(aid)

    @app.get("/api/analyses/{aid}/budget")
    def analysis_budget(aid: str):
        row = ingest.store.get("analysis", aid)
        return analyses.budget(row["job_id"], row["episode_count"], "full")

    @app.get("/api/task-templates")
    def task_templates():
        return TemplateLibrary(ingest.store).read()

    @app.post("/api/task-templates")
    def save_task_templates(body: TemplateRequest):
        return TemplateLibrary(ingest.store).save(body)

    @app.get("/api/subtask-review/{dataset_id}/episodes/{episode}")
    def review_episode(dataset_id: str, episode: int):
        return subtask_review.episode(dataset_id, episode)

    @app.post("/api/subtask-review/{dataset_id}/episodes/{episode}")
    def review_save(dataset_id: str, episode: int, body: DraftRequest):
        return subtask_review.save(dataset_id, episode, body)

    @app.post("/api/subtask-review/{dataset_id}/exports", status_code=202)
    def review_export(dataset_id: str, body: RevisionExportRequest):
        job = runner.subtask_exports.enqueue_review(dataset_id, body)
        return {k: job.get(k) for k in ("id", "state", "stage", "dataset_name", "error")}

    @app.get("/api/subtask-review/{dataset_id}/exports/{export_id}")
    def review_export_status(dataset_id: str, export_id: str):
        subtask_review.resolve(dataset_id)
        job = ingest.store.get("job", export_id)
        if job.get("source_kind") != "lerobot_subtask_revision" or job.get("source_job_id") != dataset_id:
            raise DomainError("修订导出任务不存在", 404)
        return {k: job.get(k) for k in ("id", "state", "stage", "dataset_name", "error")}

    @app.get("/api/annotations/options")
    def annotation_options():
        return annotations.options()

    @app.get("/api/annotations/candidates")
    def annotation_candidates():
        return {"datasets": annotations.candidates()}

    @app.post("/api/annotations/models")
    def annotation_models(body: AnnotationModelsRequest):
        return annotations.models(base_url=body.base_url, api_key=body.api_key)

    @app.get("/api/annotations")
    def annotation_list():
        rows = sorted(
            ingest.store.all("annotation"),
            key=lambda row: float(row.get("created_at") or 0),
            reverse=True,
        )
        return rows

    @app.post("/api/annotations", status_code=202)
    def annotation_start(body: AnnotationRequest):
        return annotations.enqueue(
            body.job_id,
            note=body.note,
            model=body.model,
            cap_usd=body.cap_usd,
            concurrency=body.concurrency,
            reasoning=body.reasoning,
            sample_fps=body.sample_fps,
            inspection=body.inspection,
            field_dictionary=body.field_dictionary,
            max_tokens=body.max_tokens,
            base_url=body.base_url,
            api_key=body.api_key,
        )

    @app.get("/api/annotations/{aid}")
    def annotation(aid: str):
        return ingest.store.get("annotation", aid)

    @app.get("/api/annotations/{aid}/budget")
    def annotation_budget(aid: str):
        row = ingest.store.get("annotation", aid)
        return annotations.budget(
            row["job_id"], int(row.get("input_episode_count") or 1)
        )

    @app.post("/api/annotations/{aid}/cancel")
    def annotation_cancel(aid: str):
        return annotations.cancel(aid)

    @app.post("/api/annotations/{aid}/resume")
    def annotation_resume(aid: str, body: AnnotationResumeRequest | None = None):
        return annotations.resume(
            aid,
            base_url=body.base_url if body else None,
            api_key=body.api_key if body else None,
        )

    @app.get("/api/annotations/{aid}/episodes")
    def annotation_episodes(aid: str):
        ingest.store.get("annotation", aid)
        return {"episodes": annotations.episodes(aid)}

    @app.get("/api/annotations/{aid}/episodes/{episode_id}")
    def annotation_episode(aid: str, episode_id: str):
        return annotations.episode(aid, episode_id)

    @app.get("/api/annotations/{aid}/episodes/{episode_id}/clips")
    def annotation_clips(aid: str, episode_id: str):
        ingest.store.get("annotation", aid)
        base = f"/api/annotations/{aid}/episodes/{episode_id}/clips"
        return {
            "clips": [
                {**row, "url": f"{base}/{quote(row['camera'], safe='')}"}
                for row in annotations.clips(aid, episode_id)
            ]
        }

    @app.get("/api/annotations/{aid}/episodes/{episode_id}/clips/{camera}")
    def annotation_clip(aid: str, episode_id: str, camera: str):
        ingest.store.get("annotation", aid)
        path = annotations.clip(aid, episode_id, camera)
        if path is None:
            raise DomainError("预览视频不存在", 404)
        # FileResponse answers byte ranges itself, which is what a <video>
        # element needs to seek inside the clip.
        return FileResponse(
            path,
            media_type="video/mp4",
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @app.get("/api/annotations/{aid}/logs")
    def annotation_log_list(aid: str):
        ingest.store.get("annotation", aid)
        return {"logs": annotations.log_names(aid)}

    @app.get("/api/annotations/{aid}/logs/{name}")
    def annotation_logs(aid: str, name: str):
        return {"text": annotations.log(aid, name)}

    @app.get("/api/annotations/{aid}/export")
    def annotation_export(aid: str):
        payload = annotations.export(aid)
        return Response(
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="annotation-{aid[:12]}.json"'
                )
            },
        )

    @app.get("/api/annotations/{aid}/subtask-export-preview")
    def subtask_export_preview(aid: str):
        return runner.subtask_exports.preview(aid)

    @app.post("/api/annotations/{aid}/subtask-exports", status_code=202)
    def subtask_export_start(aid: str, body: SubtaskExportRequest | None = None):
        return runner.subtask_exports.enqueue(aid, body.name if body else None)

    @app.get("/api/checks/{cid}/logs/{name}")
    def check_logs(cid: str, name: str):
        if name not in {"validate", "doctor"}:
            raise DomainError("日志不存在", 404)
        row = ingest.store.get("check", cid)
        job = ingest.store.get("job", row["job_id"])
        path = (
            storage_path(settings, job, "work", job["id"])
            / "checks"
            / cid
            / f"{name}.stderr.log"
        )
        return {
            "text": path.read_text(errors="replace")[-16000:]
            if path.exists()
            else "尚无日志"
        }

    @app.get("/api/jobs/{jid}/logs/{name}")
    def logs(jid: str, name: str):
        if name not in {
            "convert",
            "validate",
            "doctor",
            "merge",
            "source-validate",
            "environment",
        }:
            raise DomainError("日志不存在", 404)
        row = ingest.store.get("job", jid)
        p = storage_path(settings, row, "work", row["id"]) / f"{name}.stderr.log"
        return {
            "text": p.read_text(errors="replace")[-16000:] if p.exists() else "尚无日志"
        }

    @app.post("/api/jobs/{jid}/viewer")
    def open_viewer(jid: str):
        with runner.lock:
            return viewer.open(ingest.store.get("job", jid))

    @app.delete("/api/jobs/{jid}/viewer")
    def close_viewer(jid: str):
        ingest.store.get("job", jid)
        return viewer.close(jid)

    @app.get("/api/viewer")
    def viewer_status():
        return viewer.status()

    @app.get("/api/uploads/{uid}/manifest")
    def manifest(uid: str):
        row = ingest.store.get("upload", uid)
        return ingest.manifest(row)

    return app
