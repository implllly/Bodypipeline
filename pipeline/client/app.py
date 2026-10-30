from __future__ import annotations

import asyncio
import io
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

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


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: str
    path: str
    request_id: str = Field(min_length=8, max_length=128)


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
    install_security(app, settings)
    mount_ui(app, replay=True)

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
    def end_batch(bid: str):
        return service.end_batch(bid)

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
