from __future__ import annotations

import asyncio
import io
import secrets
import time
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from anyio import CancelScope
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from pipeline.shared.core import DomainError, ProcessLock
from pipeline.shared.security import install_security
from pipeline.shared.web import mount_ui

from .replay import ReplayService
from .service import CaptureService
from .upload import UploadAgent


class CaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_name: str | None = Field(default=None, min_length=1, max_length=2000)
    instruction: str = Field(default="", max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=128)
    local_root: str = "default"
    local_prefix: str = ""
    remote_target: str = "default"
    remote_prefix: str = ""
    capture_options: dict[str, StrictBool] = Field(default_factory=dict)


class CaptureOptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    options: dict[str, StrictBool]


class EpisodeCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(min_length=1, max_length=32)
    request_id: str = Field(min_length=8, max_length=64)


class EndBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    force: bool = False


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: str
    path: str
    request_id: str = Field(min_length=8, max_length=128)


class NativeCollectionChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["start", "stop"]


class NativeCollectionDatasetRequest(BaseModel):
    # The upstream component includes raw_only/camera_depth in its read-only
    # dataset preflight even though the native server ignores those fields.
    model_config = ConfigDict(extra="ignore")
    subdirectory: str = Field(default="", max_length=64)
    dataset_name: str = Field(min_length=1, max_length=64)
    task_prompt: str = Field(min_length=1, max_length=400)


class NativeCollectionStartRequest(NativeCollectionDatasetRequest):
    raw_only: StrictBool
    camera_depth: StrictBool
    nonce: str = Field(min_length=8, max_length=128)
    resume_dataset: bool = False


class NativeCollectionStopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nonce: str = Field(min_length=8, max_length=128)


class NativeCollectionEpisodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=8, max_length=64)


class NativeCollectionCameraDepthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    depth: StrictBool


def create_app(settings, background=True):
    service = CaptureService(settings)
    replay = ReplayService(settings, service)
    service.replay = replay
    uploader = UploadAgent(service)
    lock = ProcessLock(settings.root / "client.lock")
    stopping = asyncio.Event()

    async def upload_loop():
        while not stopping.is_set():
            await asyncio.to_thread(uploader.process_once)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=settings.poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def seal_loop():
        while not stopping.is_set():
            await asyncio.to_thread(service.seal_pending)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass

    async def health_loop():
        while not stopping.is_set():
            await asyncio.to_thread(service.sync_external)
            for row in service.store.all("capture"):
                if row["state"] == "RECORDING" and not row.get("external_trigger"):
                    import shutil

                    status = await asyncio.to_thread(service.health)
                    low_disk = (
                        shutil.disk_usage(row["raw_root"]).free
                        < settings.min_free_bytes
                    )
                    failed = status.get("status", {}).get("fail", False)
                    if low_disk or failed:
                        await asyncio.to_thread(
                            service.stop,
                            row["id"],
                            fault="磁盘低水位" if low_disk else "Recorder 报告失败",
                        )
            try:
                await asyncio.wait_for(stopping.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

    @asynccontextmanager
    async def lifespan(app):
        lock.acquire()
        service.recover()
        tasks = (
            [
                asyncio.create_task(upload_loop()),
                asyncio.create_task(health_loop()),
                asyncio.create_task(seal_loop()),
            ]
            if background
            else []
        )
        try:
            yield
        finally:
            stopping.set()
            service.background.stop.set()
            if tasks:
                await asyncio.gather(*tasks)
            await asyncio.to_thread(replay.close)
            service.adapter.close()
            lock.release()

    app = FastAPI(title="Embodied Pipeline Client", lifespan=lifespan)
    app.state.service = service
    app.state.replay = replay
    app.state.uploader = uploader
    native_client_page = getattr(
        service.adapter, "native_client_html", None
    )
    app.state.native_client_page = callable(native_client_page)
    install_security(app, settings)
    mount_ui(app, replay=True, index_page=native_client_page)
    native_challenges = {}

    @app.get("/api/replay/status")
    def replay_status():
        return replay.status()

    @app.get("/api/replay/captures")
    def replay_captures():
        return replay.captures()

    @app.get("/api/replay/files")
    def replay_files(root: str, directory: str = ""):
        return replay.files(root, directory)

    @app.get("/api/replay/inspect")
    def replay_inspect(root: str, path: str):
        return replay.inspect(root, path)

    @app.post("/api/replay/start", status_code=202)
    def replay_start(body: ReplayRequest):
        return replay.start(body.root, body.path, body.request_id)

    @app.post("/api/replay/stop", status_code=202)
    def replay_stop():
        return replay.stop()

    @app.get("/healthz")
    def live():
        return {"service": "client", "alive": True}

    @app.get("/api/config")
    def config():
        return {
            "role": "client",
            "mode": settings.mode,
            "robot_id": settings.robot_id,
            "profile": service.profile,
            "capture_capabilities": service.capabilities,
            "local_roots": [
                {"id": k, "path": str(v)} for k, v in service.roots.items()
            ],
            "server_url": settings.server_url,
        }

    @app.get("/api/health")
    def health():
        return {
            **service.health(),
            "background_transfer": service.background.status(),
            "batch": service.current_batch(),
        }

    @app.get("/api/capture-batches")
    def batches():
        return service.store.all("batch")

    @app.get("/api/capture-service")
    def capture_service():
        return service.capture_service_status()

    @app.get("/capture-visualizer/", response_class=HTMLResponse)
    def native_capture_visualizer():
        reader = getattr(service.adapter, "native_visualizer_html", None)
        if reader is None:
            raise DomainError("当前 Adapter 未提供原生可视化页面", 404)
        return HTMLResponse(
            reader(),
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/capture-visualizer/api/status")
    def native_visualizer_status():
        reader = getattr(service.adapter, "telemetry_status", None)
        if reader is None:
            raise DomainError("当前 Adapter 未配置采集遥测", 501)
        return reader()

    @app.get("/capture-visualizer/api/signals")
    def native_visualizer_signals():
        return capture_telemetry_signals()

    @app.get("/capture-visualizer/api/history")
    def native_visualizer_history(signal: list[str] = Query(default=[])):
        return capture_telemetry_history(signal)

    if callable(native_client_page):

        def consume_native_challenge(nonce, operation):
            now = time.monotonic()
            for key, value in list(native_challenges.items()):
                if value[1] <= now:
                    native_challenges.pop(key, None)
            challenge = native_challenges.pop(nonce, None)
            if challenge is None or challenge[0] != operation or challenge[1] <= now:
                raise DomainError("采集操作确认已失效，请重新操作", 409)

        @app.get("/capture-visualizer/api/collection/status")
        def native_collection_status():
            return service.capture_service_status()

        @app.post("/capture-visualizer/api/collection/dataset")
        def native_collection_dataset(body: NativeCollectionDatasetRequest):
            reader = getattr(service.adapter, "collection_dataset", None)
            if reader is None:
                raise DomainError("当前 Adapter 未提供数据集预检", 501)
            return reader(body.model_dump())

        @app.post("/capture-visualizer/api/collection/challenge")
        def native_collection_challenge(body: NativeCollectionChallengeRequest):
            nonce = secrets.token_urlsafe(24)
            native_challenges[nonce] = (body.operation, time.monotonic() + 30)
            return {"nonce": nonce}

        @app.post("/capture-visualizer/api/collection/start")
        def native_collection_start(body: NativeCollectionStartRequest):
            consume_native_challenge(body.nonce, "start")
            if body.raw_only is not True:
                raise DomainError(
                    "BodyPipeline Client 仅支持保存 Raw，再由 Server 转换", 400
                )
            roots = list(service.roots)
            if not roots:
                raise DomainError("Client 未配置本地采集目录", 500)
            result = service.begin_batch(
                {
                    "dataset_name": body.dataset_name,
                    "instruction": body.task_prompt,
                    "idempotency_key": body.nonce,
                    "local_root": roots[0],
                    "local_prefix": body.subdirectory,
                    "remote_target": "default",
                    "remote_prefix": "",
                    "capture_options": {
                        "raw_only": True,
                        "camera_depth": body.camera_depth,
                    },
                }
            )
            if result.get("state") != "ACTIVE":
                raise DomainError(
                    result.get("error") or "采集批次未进入活动状态", 409
                )
            return service.capture_service_status()

        @app.post("/capture-visualizer/api/collection/stop")
        def native_collection_stop(body: NativeCollectionStopRequest):
            consume_native_challenge(body.nonce, "stop")
            batch = service.current_batch()
            if batch is None:
                raise DomainError("当前没有活动采集批次", 409)
            service.end_batch(batch["id"], force=True)
            return service.capture_service_status()

        @app.post("/capture-visualizer/api/collection/camera-depth")
        def native_collection_camera_depth(
            body: NativeCollectionCameraDepthRequest,
        ):
            state = service.configure_capture_service(
                {"raw_only": True, "camera_depth": body.depth}
            )
            return state.get("camera", state)

        @app.post("/capture-visualizer/api/collection/episodes/{action}")
        def native_collection_episode(
            action: Literal["start", "stop", "discard"],
            body: NativeCollectionEpisodeRequest,
        ):
            batch = service.current_batch()
            if batch is None:
                raise DomainError("当前没有活动采集批次", 409)
            return service.batch_command(batch["id"], action, body.request_id)

    @app.post("/api/capture-service/prepare")
    def prepare_capture_service():
        return service.prepare_capture_service()

    @app.patch("/api/capture-service/options")
    def configure_capture_service(body: CaptureOptionsRequest):
        return service.configure_capture_service(body.options)

    @app.get("/api/capture-telemetry/signals")
    def capture_telemetry_signals():
        telemetry = service.capabilities.get("telemetry")
        reader = getattr(service.adapter, "telemetry_signals", None)
        if not telemetry or reader is None:
            raise DomainError("当前 Adapter 未配置采集遥测", 501)
        return reader()

    @app.get("/api/capture-telemetry/history")
    def capture_telemetry_history(signal: list[str] = Query(default=[])):
        telemetry = service.capabilities.get("telemetry")
        reader = getattr(service.adapter, "telemetry_history", None)
        if not telemetry or reader is None:
            raise DomainError("当前 Adapter 未配置采集遥测", 501)
        maximum = telemetry.get("max_signals", 16)
        if (
            not signal
            or len(signal) > maximum
            or len(set(signal)) != len(signal)
            or any(not item or len(item) > 240 for item in signal)
        ):
            raise DomainError(f"请选择 1-{maximum} 个不同的遥测信号", 400)
        return reader(signal)

    @app.post("/api/capture-batches/{bid}/commands")
    def batch_command(bid: str, body: EpisodeCommandRequest):
        return service.batch_command(bid, body.action, body.request_id)

    @app.post("/api/capture-batches", status_code=201)
    def begin_batch(body: CaptureRequest):
        data = body.model_dump(exclude_none=True)
        data["instruction"] = data["instruction"].strip() or data.get(
            "dataset_name", ""
        )
        if not data["instruction"]:
            raise DomainError("请填写本批次任务描述", 400)
        return service.begin_batch(data)

    @app.post("/api/capture-batches/{bid}/end")
    def end_batch(bid: str, body: EndBatchRequest | None = None):
        return service.end_batch(bid, force=bool(body and body.force))

    @app.get("/api/targets")
    def targets():
        try:
            with httpx.Client(timeout=5, trust_env=False) as http:
                r = http.get(
                    settings.server_url + "/device/targets",
                    headers={"Authorization": f"Bearer {settings.device_token}"},
                )
                r.raise_for_status()
                data = r.json()
                service.store.put("cache", "targets", data)
                return {**data, "cached": False}
        except Exception:
            try:
                return {**service.store.get("cache", "targets"), "cached": True}
            except DomainError:
                return {"targets": [], "cached": True}

    @app.get("/api/captures")
    def captures():
        return [service.describe(row) for row in service.store.all("capture")]

    @app.get("/api/capture-status")
    def capture_status():
        # Keep polling small: full profiles and manifests are not needed by the UI.
        omitted = {
            "profile",
            "profile_hash",
            "request_hash",
            "idempotency_key",
            "receipt",
            "manifest_hash",
        }
        return [
            {k: v for k, v in service.describe(row).items() if k not in omitted}
            for row in service.store.all("capture")
        ]

    @app.post("/api/captures/start", status_code=201)
    def create_and_start(body: CaptureRequest):
        row = create(body)
        return service.start(row["id"])

    @app.post("/api/captures", status_code=201)
    def create(body: CaptureRequest):
        data = body.model_dump(exclude_none=True)
        data["instruction"] = data["instruction"].strip()
        if not (data.get("dataset_name") or data["instruction"]):
            raise DomainError("请填写数据集名称", 400)
        return service.create(data)

    @app.get("/api/captures/{cid}")
    def get(cid: str):
        return service.store.get("capture", cid)

    @app.post("/api/captures/{cid}/start")
    def start(cid: str):
        return service.start(cid)

    @app.post("/api/captures/{cid}/stop")
    def stop(cid: str):
        return service.stop(cid)

    @app.post("/api/captures/{cid}/discard")
    def discard(cid: str):
        return service.stop(cid, discard=True)

    @app.post("/api/captures/{cid}/keep", status_code=202)
    def keep(cid: str):
        return service.request_keep(cid)

    @app.post("/api/captures/{cid}/retry-upload")
    def retry(cid: str):
        with service.lock:
            row = service.store.get("capture", cid)
            if row["upload_state"] != "RETRY":
                raise DomainError("只有等待重试的上传可以重排队")
            row.update(upload_state="QUEUED", retry_at=0)
            return service.save(row)

    @app.get("/api/cameras/{camera_id}/stream")
    async def camera_stream(camera_id: str):
        if camera_id not in {c["id"] for c in service.profile["cameras"]}:
            raise DomainError("相机不存在", 404)
        opener = getattr(service.adapter, "open_preview", None)
        if not service.capabilities.get("preview_stream") or opener is None:
            raise DomainError("当前 Adapter 未配置连续相机预览", 501)
        context = opener(camera_id)
        source = await context.__aenter__()

        async def chunks():
            try:
                async for chunk in source.chunks:
                    yield chunk
            except (httpx.HTTPError, DomainError):
                # Headers have already been sent; closing lets the UI reconnect.
                return
            finally:
                with CancelScope(shield=True):
                    await context.__aexit__(None, None, None)

        return StreamingResponse(
            chunks(), media_type=source.content_type,
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/capture-visualizer/api/camera/{filename}")
    async def native_camera_stream(filename: str):
        if not filename.endswith(".mjpg"):
            raise DomainError("原生相机流路径无效", 404)
        source_key = filename.removesuffix(".mjpg")
        camera = next(
            (
                item
                for item in service.profile["cameras"]
                if item.get("source_key") == source_key
            ),
            None,
        )
        if camera is None:
            raise DomainError("相机不存在", 404)
        return await camera_stream(camera["id"])

    @app.get("/api/cameras/{camera_id}/frame")
    def frame(camera_id: str, preview: Literal["full", "low"] = "full"):
        if camera_id not in {c["id"] for c in service.profile["cameras"]}:
            raise DomainError("相机不存在", 404)
        content = service.adapter.frame(camera_id)
        if not content:
            raise DomainError("暂未收到相机图像", 503)
        if preview == "low" and not service.capabilities.get("preview_passthrough"):
            # Preview only; recorded images remain at their original resolution.
            with Image.open(io.BytesIO(content)) as image:
                image.thumbnail((320, 320))
                output = io.BytesIO()
                image.convert("RGB").save(output, format="JPEG", quality=45)
                content = output.getvalue()
        return Response(
            content, media_type="image/jpeg", headers={"Cache-Control": "no-store"}
        )

    return app
