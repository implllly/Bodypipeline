from __future__ import annotations

import asyncio
import html
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field, StrictInt

from pipeline.shared.core import DomainError, Store, executable_path
from pipeline.shared.security import install_security
from pipeline.shared.web import themed_html

from .datasets import dataset_for_job
from .episode_scores import EpisodeScoreCache
from .recycle import RecycleBin
from .subtask_review import DraftRequest, RevisionExportRequest
from .task_annotations import TemplateRequest
from .viewer_data import PreviewRegistry, serve_dataset_file

SERVICE_KEY = "service"
SERVICE_KIND = "viewer"


class ViewerManager:
    """Keeps one read-only Visualizer running and registers datasets for it.

    The Visualizer application is dataset-agnostic: it fetches
    ``<origin>/<org>/<dataset>/resolve/main/...``. The gateway serves that URL
    shape for every registered dataset, so opening a preview only records a
    mapping. No process is started or stopped per dataset, which removes the
    single-session conflict and the port races that came with it.
    """

    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.process = None
        self.log = None
        self.lock = threading.RLock()
        self.registry = PreviewRegistry(store, settings.viewer_ttl)

    # ------------------------------------------------------------ lifecycle
    def _ports_busy(self):
        for port in (self.settings.viewer_ui_port,):
            with socket.socket() as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    return True
        return False

    def _service(self):
        try:
            return self.store.get(SERVICE_KIND, SERVICE_KEY)
        except DomainError as exc:
            if exc.code == 404:
                return None
            raise

    def _put_service(self, **fields):
        row = self._service() or {}
        row.update(fields)
        self.store.put(SERVICE_KIND, SERVICE_KEY, row)
        return row

    def _kill_group(self, pid):
        if not isinstance(pid, int) or pid <= 1:
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.killpg(pid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.1)
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _reap_matching_processes(self):
        """Kill our own Visualizer processes that predate PID tracking.

        Only processes whose command line names this server's viewer cache
        directory are touched; anything else on the port is left alone.
        """

        marker = str(self.settings.root / "viewer-cache").encode()
        proc = Path("/proc")
        if not proc.is_dir():
            return
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
            except OSError:
                continue
            pid = int(entry.name)
            if marker not in cmdline or pid <= 1 or pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                continue
        deadline = time.time() + 10
        while time.time() < deadline and self._ports_busy():
            time.sleep(0.1)

    def _clear_stale(self):
        if self.process is not None or not self._ports_busy():
            return
        service = self._service() or {}
        if service.get("pid"):
            self._kill_group(service["pid"])
        if self._ports_busy():
            self._reap_matching_processes()

    def recover(self):
        """Reap any Visualizer a previous server process left behind."""

        service = self._service()
        if service is None:
            return
        if service.get("state") in {"STARTING", "READY"}:
            self._kill_group(service.get("pid"))
            if self._ports_busy():
                self._reap_matching_processes()
            self._put_service(
                state="INTERRUPTED",
                error="Server 重启，已清理上一次的预览进程",
                pid=None,
            )

    def ensure_service(self):
        """Start the Visualizer if it is not already running."""

        with self.lock:
            if self.process and self.process.poll() is None:
                return self._service()
            executable = executable_path(self.settings.letools_bin)
            if not executable:
                raise DomainError("未安装 letools / Visualizer 依赖")
            self._clear_stale()
            if self._ports_busy():
                raise DomainError(
                    f"预览内部端口 {self.settings.viewer_ui_port} 已被其他程序占用，不自动终止其他进程"
                )
            log_path = self.settings.root / "reports/viewer.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log = log_path.open("ab")
            env = self.settings.tool_environment()
            env["NEXT_TELEMETRY_DISABLED"] = "1"
            wrapper = [
                sys.executable,
                "-m",
                "pipeline.server.viewer_bootstrap",
                "--executable",
                executable,
                "--cache",
                str(self.settings.root / "viewer-cache"),
                "--data-origin",
                self.settings.viewer_origin.rstrip("/") + "/_data",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.settings.viewer_ui_port),
            ]
            if self.settings.viewer_bun:
                wrapper += ["--bun", str(Path(self.settings.viewer_bun).resolve())]
            self.process = subprocess.Popen(
                wrapper,
                stdout=self.log,
                stderr=self.log,
                start_new_session=True,
                env=env,
            )
            return self._put_service(
                state="STARTING",
                pid=self.process.pid,
                log_path=str(log_path),
                started_at=time.time(),
                error=None,
            )

    def autostart(self):
        """Bring the always-on Visualizer up; record why if it cannot start."""

        with self.lock:
            try:
                self.ensure_service()
            except DomainError as exc:
                self._put_service(state="UNAVAILABLE", pid=None, error=exc.message)
            except (OSError, subprocess.SubprocessError) as exc:
                self._put_service(state="FAILED", pid=None, error=str(exc))

    def poll(self):
        """Mark the service READY, restart it if it died, and prune old links."""

        with self.lock:
            self.registry.prune()
            service = self._service()
            if self.process is None:
                if (
                    service
                    and service.get("state") == "FAILED"
                    and time.time() >= service.get("retry_at", 0)
                ):
                    self.autostart()
                return
            if self.process.poll() is not None:
                code = self.process.returncode
                self.process = None
                if self.log:
                    self.log.close()
                    self.log = None
                self._put_service(
                    state="FAILED",
                    pid=None,
                    retry_at=time.time() + 30,
                    error=f"Visualizer 退出：{code}；检查 reports/viewer.log",
                )
                return
            if service and service.get("state") == "STARTING":
                try:
                    response = httpx.get(
                        f"http://127.0.0.1:{self.settings.viewer_ui_port}/",
                        timeout=1,
                        trust_env=False,
                    )
                    if response.status_code < 500:
                        self._put_service(state="READY")
                except httpx.HTTPError:
                    pass

    def stop(self):
        """Stop the Visualizer. Never depends on readable state, so shutdown is safe."""

        with self.lock:
            if self.process and self.process.poll() is None:
                self._kill_group(self.process.pid)
                try:
                    self.process.wait(5)
                except subprocess.TimeoutExpired:
                    pass
            self.process = None
            deadline = time.time() + 10
            while time.time() < deadline and self._ports_busy():
                time.sleep(0.1)
            if self.log:
                self.log.close()
                self.log = None
            try:
                return self._put_service(state="STOPPED", pid=None)
            except DomainError:
                # A corrupt state file must not abort shutdown or hold the lock.
                return {"state": "STOPPED"}

    # --------------------------------------------------------------- opening
    def open(self, job):
        """Register ``job``'s dataset and return the link that shows it."""

        data = dataset_for_job(self.settings, job)
        service = self.ensure_service()
        row = self.registry.register(job["id"], data)
        return self._describe(row, service)

    def close(self, job_id):
        self.registry.remove(job_id)
        return {"state": "CLOSED", "job_id": job_id}

    def _describe(self, row, service):
        origin = self.settings.viewer_origin.rstrip("/")
        return {
            "state": (service or {}).get("state", "STARTING"),
            "job_id": row["job_id"],
            "url": origin + row["page_path"],
            "expires_at": row["expires_at"],
        }

    def status(self):
        service = self._service() or {"state": "STOPPED"}
        return {
            "state": service.get("state", "STOPPED"),
            "error": service.get("error"),
            "previews": [
                self._describe(row, service) for row in self.registry.active()
            ],
        }


def _gateway_state(store):
    try:
        return store.get(SERVICE_KIND, SERVICE_KEY)
    except DomainError as exc:
        if exc.code == 404:
            return None
        raise


STARTING_PAGE = themed_html("""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="2">
<title>正在启动数据预览</title></head>
<body style="margin:0;background:var(--page-bg,#f7f8fa);color:var(--page-ink,#292e36);font:15px system-ui,'PingFang SC','Microsoft YaHei',sans-serif">
<main class="viewer-starting-card" style="max-width:620px;margin:15vh auto;padding:32px;background:#fff;border:1px solid #e0e3e8;border-radius:8px">
<h1 style="font-size:22px;margin:0 0 12px">正在启动 LeRobot Visualizer</h1>
<p style="line-height:1.7;margin:0;color:#6b7280">首次启动可能需要安装或准备前端依赖。页面每 2 秒自动刷新，就绪后会直接显示数据集。</p>
</main></body></html>""")


def _doctor_html(payload: dict, dataset_path: str, sample: str) -> str:
    """Render the Doctor payload with the shared workbench theme."""

    severity = str(payload.get("overall_severity", "PASS"))
    sections = []
    for check in payload.get("checks", []):
        messages = "".join(
            "<li><span class='message-severity'>"
            + html.escape(str(m.get("severity", "")))
            + "</span> "
            + html.escape(str(m.get("message", "")))
            + "</li>"
            for m in check.get("messages", [])
        )
        sections.append(
            "<section class='doctor-check'><div class='check-head'><h2>"
            + html.escape(str(check.get("name", "Check")))
            + "</h2><span class='severity "
            + html.escape(str(check.get("severity", "PASS")).lower())
            + "'>"
            + html.escape(str(check.get("severity", "PASS")))
            + "</span></div><ul>"
            + messages
            + "</ul></section>"
        )
    return themed_html(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>LeRobot Doctor</title>
<style>
:root {{ color-scheme: light; }}
body {{ margin: 0; background: #f7f8fa; color: #292e36;
font-family: Inter, ui-sans-serif, system-ui, "PingFang SC", "Microsoft YaHei", sans-serif; }}
header {{ padding: 20px 24px 16px; border-bottom: 1px solid #e8ebf1; background: #fff; }}
h1 {{ margin: 0 0 6px; font-size: 20px; }}
.meta {{ font-size: 13px; color: #6b7280; }}
main {{ padding: 16px 24px 32px; display: grid; gap: 10px; }}
section {{ border: 1px solid #e3e7ee; border-radius: 6px; background: #fff; padding: 12px 14px; }}
.check-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
h2 {{ margin: 0; font-size: 14px; color: #3b434f; }}
.severity {{ border: 1px solid currentColor; border-radius: 4px; padding: 2px 7px;
font: 700 11px ui-monospace, monospace; }}
.pass {{ color: #258467; }} .warn {{ color: #9a741b; }} .fail {{ color: #c04b59; }}
ul {{ margin: 9px 0 0; padding-left: 19px; font-size: 12px; line-height: 1.55; color: #4f5866; }}
.message-severity {{ color: #8a919d; font: 700 10px ui-monospace, monospace; }}
</style></head><body><header class="doctor-header"><h1>LeRobot Doctor
<span class="severity {html.escape(severity.lower())}">{html.escape(severity)}</span></h1>
<div class="meta">{html.escape(dataset_path)} &middot; {html.escape(sample)} &middot;
{html.escape(str(payload.get("total_episodes", "?")))} episodes &middot;
{html.escape(str(payload.get("total_frames", "?")))} frames</div></header>
<main>{"".join(sections)}</main></body></html>""")


class DoctorCache:
    """Computes Doctor once per dataset and reuses it until the link expires."""

    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.Lock()
        self.cache: dict[str, tuple[float, dict]] = {}

    def payload(self, row: dict) -> dict:
        key = row["dataset_path"]
        stamp = (Path(key) / "meta/info.json").stat().st_mtime
        with self.lock:
            cached = self.cache.get(key)
            if cached and cached[0] == stamp:
                return cached[1]
        tool = executable_path(self.settings.letools_bin)
        if not tool:
            raise DomainError("未安装 letools / Doctor 依赖", 503)
        result = subprocess.run(
            [tool, "doctor", "check", key, "--json", "--max-episodes", "20"],
            capture_output=True,
            text=True,
            timeout=self.settings.command_timeout,
            env=self.settings.tool_environment(),
        )
        # lerobot-doctor exits 1 on a FAIL severity but still prints a report.
        if result.returncode not in (0, 1):
            raise DomainError("Doctor 运行失败：" + result.stderr[-400:], 502)
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            raise DomainError("Doctor 未返回有效报告", 502) from exc
        with self.lock:
            self.cache[key] = (stamp, payload)
        return payload


class RecycleSelection(BaseModel):
    episodes: list[StrictInt] = Field(min_length=1, max_length=100000)
    revision: StrictInt = Field(ge=0)


def create_gateway(settings):
    settings.role = "viewer"
    store = Store(settings.root / "state", legacy_name="server.sqlite")
    registry = PreviewRegistry(store, settings.viewer_ttl)
    doctor = DoctorCache(settings)
    scores = EpisodeScoreCache(settings, store)
    recycle = RecycleBin(settings, store, registry)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await asyncio.to_thread(scores.close)

    app = FastAPI(title="LeRobot Viewer Gateway", lifespan=lifespan)
    app.state.episode_scores = scores
    # The gateway is reachable beyond loopback once it is bound to a routable
    # address, and it serves raw dataset files, so it shares the Server's token.
    install_security(app, settings)

    @app.exception_handler(DomainError)
    async def domain_error(_request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.code)

    def service_ready():
        state = _gateway_state(store)
        return state is not None and state.get("state") == "READY"

    def score_preview(dataset_id):
        row = registry.lookup(f"bp/{dataset_id}")
        if row is None:
            raise DomainError("预览已过期或不存在，请在管理台重新打开", 404)
        return row

    async def review_proxy(dataset_id, suffix, body=None):
        # Only server-owned preview identities are accepted, never a local_path
        # or arbitrary proxy destination. The Server alone writes draft state.
        score_preview(dataset_id)
        return await annotation_proxy(f"/api/subtask-review/{dataset_id}/{suffix}", body)

    async def annotation_proxy(route, body=None):
        url = settings.server_url.rstrip("/") + route
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.request(
                    "GET" if body is None else "POST", url,
                    json=body.model_dump() if body is not None else None,
                    headers={"Authorization": "Bearer " + settings.token},
                    timeout=settings.command_timeout,
                )
        except httpx.HTTPError as exc:
            raise DomainError("Server 标注服务暂不可用，请稍后重试", 503) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise DomainError("Server 标注服务返回无效响应", 502) from exc
        return JSONResponse(payload, status_code=response.status_code, headers={"Cache-Control": "no-store"})

    @app.get("/api/task-templates")
    async def task_templates():
        return await annotation_proxy("/api/task-templates")

    @app.post("/api/task-templates")
    async def save_task_templates(body: TemplateRequest):
        return await annotation_proxy("/api/task-templates", body)

    @app.get("/api/subtask-review/{dataset_id}/episodes/{episode}")
    async def review_episode(dataset_id: str, episode: int):
        return await review_proxy(dataset_id, f"episodes/{episode}")

    @app.post("/api/subtask-review/{dataset_id}/episodes/{episode}")
    async def review_save(dataset_id: str, episode: int, body: DraftRequest):
        return await review_proxy(dataset_id, f"episodes/{episode}", body)

    @app.post("/api/subtask-review/{dataset_id}/exports")
    async def review_export(dataset_id: str, body: RevisionExportRequest):
        return await review_proxy(dataset_id, "exports", body)

    @app.get("/api/subtask-review/{dataset_id}/exports/{export_id}")
    async def review_export_status(dataset_id: str, export_id: str):
        if not export_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in export_id):
            raise DomainError("修订导出任务编号无效", 404)
        return await review_proxy(dataset_id, f"exports/{export_id}")

    @app.get("/api/episode-scores/{dataset_id}")
    async def score_status(dataset_id: str):
        row = score_preview(dataset_id)
        result = await asyncio.to_thread(scores.status, row)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.post("/api/episode-scores/{dataset_id}")
    async def score_start(dataset_id: str):
        row = score_preview(dataset_id)
        result = await asyncio.to_thread(scores.start, row)
        return JSONResponse(
            result, status_code=202, headers={"Cache-Control": "no-store"}
        )

    @app.get("/api/recycle/{dataset_id}")
    async def recycle_status(dataset_id: str):
        result = await asyncio.to_thread(recycle.status, f"bp/{dataset_id}")
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.post("/api/recycle/{dataset_id}/{action}")
    async def recycle_change(dataset_id: str, action: str, selection: RecycleSelection):
        if action not in {"trash", "restore", "purge"}:
            raise DomainError("不支持的回收站操作", 404)
        result = await asyncio.to_thread(
            recycle.change,
            f"bp/{dataset_id}",
            action,
            sorted(set(selection.episodes)),
            selection.revision,
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/bp/{dataset_id}/trash")
    async def recycle_page(dataset_id: str):
        await asyncio.to_thread(recycle.resolve, f"bp/{dataset_id}")
        page = Path(__file__).with_name("viewer_ui") / "recycle.html"
        return HTMLResponse(
            themed_html(page.read_text()), headers={"Cache-Control": "no-store"}
        )

    async def data_response(request: Request, path: str):
        """``/_data/<org>/<dataset>/resolve/main/<file>`` plus the Doctor report.

        The Visualizer's Doctor tab embeds ``/_data/doctor/?dataset=<org>/<id>``;
        ``report.json`` returns the same payload for automation.
        """

        rest = path[len("_data/") :] if path.startswith("_data/") else ""
        route = rest.strip("/")
        if route in {"doctor", "doctor/report.json"}:
            repo_id = request.query_params.get("dataset", "")
            row = registry.lookup(repo_id)
            if row is None:
                return JSONResponse(
                    {"detail": "预览已过期或不存在，请在管理台重新打开"},
                    status_code=404,
                )
            payload = await asyncio.to_thread(doctor.payload, row)
            if route.endswith("report.json"):
                return JSONResponse(payload, headers={"Cache-Control": "no-store"})
            return HTMLResponse(
                _doctor_html(payload, row["dataset_path"], "up to 20 episodes"),
                headers={"Cache-Control": "no-store"},
            )
        return serve_dataset_file(request, registry, rest)

    @app.get("/")
    async def index():
        rows = registry.active()
        if not rows:
            return JSONResponse(
                {"detail": "预览尚未就绪或已过期，请在管理台重新打开"},
                status_code=503,
            )
        return RedirectResponse(
            rows[0]["page_path"],
            status_code=307,
            headers={"Cache-Control": "no-store"},
        )

    @app.api_route("/{path:path}", methods=["GET", "HEAD"])
    async def proxy(request: Request, path: str):
        if path == "_data" or path.startswith("_data/"):
            return await data_response(request, path)
        parts = path.split("/")
        if (
            service_ready()
            and len(parts) == 3
            and parts[0] == "bp"
            and registry.lookup(f"bp/{parts[1]}") is not None
            and parts[2].startswith("episode_")
        ):
            episode_text = parts[2].removeprefix("episode_")
            if episode_text.isdigit():
                status = await asyncio.to_thread(recycle.status, f"bp/{parts[1]}")
                if int(episode_text) >= status["total_episodes"] or any(
                    e["episode"] == int(episode_text) for e in status["entries"]
                ):
                    return RedirectResponse(
                        f"/bp/{parts[1]}/trash",
                        status_code=303,
                        headers={"Cache-Control": "no-store"},
                    )
        if not service_ready():
            if request.headers.get("accept", "").startswith("text/html"):
                return HTMLResponse(
                    STARTING_PAGE, headers={"Cache-Control": "no-store"}
                )
            return JSONResponse({"detail": "预览仍在启动，请稍后刷新"}, status_code=503)
        url = f"http://127.0.0.1:{settings.viewer_ui_port}/" + path
        if request.url.query:
            url += "?" + request.url.query
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower()
            in {
                "range",
                "if-range",
                "accept",
                "accept-encoding",
                "if-none-match",
                "rsc",
                "next-router-state-tree",
                "next-router-prefetch",
                "next-url",
            }
        }
        client = httpx.AsyncClient(timeout=120, trust_env=False)
        try:
            response = await client.send(
                client.build_request(request.method, url, headers=headers),
                stream=True,
                follow_redirects=False,
            )
        except Exception:
            await client.aclose()
            raise DomainError("预览后端不可达", 502)

        async def chunks():
            try:
                async for data in response.aiter_raw():
                    yield data
            finally:
                await response.aclose()
                await client.aclose()

        outgoing = {
            k: v
            for k, v in response.headers.items()
            if k.lower()
            in {
                "content-type",
                "content-length",
                "content-range",
                "accept-ranges",
                "content-encoding",
                "etag",
                "vary",
                "location",
                "cache-control",
            }
        }
        return StreamingResponse(
            chunks(), status_code=response.status_code, headers=outgoing
        )

    return app


__all__ = ["ViewerManager", "create_gateway"]
