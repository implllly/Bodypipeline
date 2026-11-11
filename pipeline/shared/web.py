from pathlib import Path
from urllib.parse import urlencode

from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.gzip import GZipMiddleware

ASSETS = Path(__file__).resolve().parents[1] / "web"
BASE_ASSETS = {"app.js", "annotations.js", "clipboard.js", "style.css", "theme.js", "theme.css", "client-replay.js"}
REPORT_ASSETS = {"report.js", "report.css"}


class ListCompressionMiddleware:
    """Negotiate compression for JSON lists without touching uploads or media."""

    def __init__(self, app, paths):
        self.app = app
        self.paths = frozenset(paths)
        self.compressed = GZipMiddleware(app, minimum_size=1024, compresslevel=3)

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope["method"] == "GET"
            and scope["path"] in self.paths
        ):
            await self.compressed(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def themed_html(page: str) -> str:
    """Share theme preferences with standalone Viewer login/Doctor pages."""

    assets = (
        "<script>" + (ASSETS / "theme.js").read_text() + "</script>"
        "<style>" + (ASSETS / "theme.css").read_text() + "</style>"
    )
    toggle = (
        '<button type="button" class="theme-toggle standalone-theme-toggle" '
        'data-theme-toggle aria-label="切换深夜与浅色模式">'
        '<span class="theme-label-light">浅色模式</span>'
        '<span class="theme-label-dark">深夜模式</span></button>'
    )
    return page.replace("</head>", assets + "</head>", 1).replace(
        "</body>", toggle + "</body>", 1
    )


def mount_ui(app, *, report=False, replay=False):
    @app.get("/")
    def index():
        return FileResponse(ASSETS / "index.html")

    if report:

        @app.get("/report")
        @app.get("/report/{job_id}")
        def report_page(job_id: str | None = None):
            return FileResponse(ASSETS / "report.html")

    if replay:

        @app.get("/replay")
        def replay_page(capture: str | None = None):
            query = "?" + urlencode({"capture": capture}) if capture else ""
            return RedirectResponse("/#replay" + query, status_code=307)

    @app.get("/assets/{name}")
    def asset(name: str):
        allowed = BASE_ASSETS | (REPORT_ASSETS if report else set())
        if name not in allowed:
            from .core import DomainError

            raise DomainError("文件不存在", 404)
        return FileResponse(ASSETS / name)
