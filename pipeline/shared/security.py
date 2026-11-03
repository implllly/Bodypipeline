from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .core import DomainError
from .web import themed_html

VIEWER_LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录数据预览 · BodyPipeline</title>
<style>
:root { font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI",
  "PingFang SC", "Microsoft YaHei", sans-serif; color: #292e36; }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center;
  background: #f7f8fa; padding: 24px; }
main { width: min(420px, 100%); background: #fff; border: 1px solid #e0e3e8;
  border-radius: 8px; padding: 30px 30px 26px; }
.brand { display: flex; align-items: center; gap: 10px; margin-bottom: 22px;
  font-size: 19px; font-weight: 700; letter-spacing: -.4px; }
.mark { display: grid; place-items: center; width: 28px; height: 30px;
  border-radius: 6px; background: #2455eb; color: #fff; font-size: 19px; }
h1 { font-size: 20px; margin: 0 0 8px; letter-spacing: -.3px; }
p.hint { margin: 0 0 20px; font-size: 12.5px; line-height: 1.75; color: #6b7280; }
label { display: block; font-size: 12px; font-weight: 550; color: #47505f;
  margin-bottom: 8px; }
input { width: 100%; padding: 10px 11px; font: inherit; font-size: 13px;
  color: #353e4d; background: #fff; border: 1px solid #d6dbe3; border-radius: 4px; }
input:focus { outline: 2px solid #2455eb; outline-offset: -1px; }
button { width: 100%; margin-top: 18px; padding: 11px 14px; font: inherit;
  font-size: 13px; font-weight: 600; color: #fff; background: #2455eb;
  border: 1px solid transparent; border-radius: 4px; cursor: pointer; }
button:hover:not(:disabled) { background: #1742c6; }
button:disabled { opacity: .55; cursor: not-allowed; }
#error { min-height: 18px; margin: 12px 0 0; font-size: 12px; color: #b4404c; }
</style></head>
<body><main class="viewer-login-card">
<div class="brand"><span class="mark">B</span> BodyPipeline</div>
<h1>登录数据预览</h1>
<p class="hint">预览服务与 Server 工作台使用同一个访问密钥（<code>PIPELINE_TOKEN</code>）。</p>
<form id="login-form">
  <label for="token">服务访问密钥</label>
  <input id="token" type="password" required autocomplete="off"
    placeholder="输入管理员配置的访问密钥" autofocus>
  <button id="submit" type="submit">进入预览</button>
  <p id="error" role="alert"></p>
</form>
</main>
<script>
const form = document.getElementById("login-form");
const error = document.getElementById("error");
const button = document.getElementById("submit");
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.textContent = "";
  button.disabled = true;
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: document.getElementById("token").value }),
    });
    if (!response.ok) {
      let detail = "登录失败";
      try { detail = (await response.json()).detail || detail; } catch {}
      throw new Error(detail);
    }
    location.reload();
  } catch (err) {
    error.textContent = err.message || String(err);
    button.disabled = false;
  }
});
</script></body></html>"""
VIEWER_LOGIN_PAGE = themed_html(VIEWER_LOGIN_PAGE)


def install_security(app: FastAPI, settings, device_prefix: str | None = None):
    cookie_name = f"pipeline_{settings.role}"
    login_password = settings.login_password or settings.token
    cookie_key = (
        hmac.new(
            settings.token.encode(),
            ("login:" + settings.login_password).encode(),
            hashlib.sha256,
        ).digest()
        if settings.login_password
        else settings.token.encode()
    )

    def valid_cookie(value):
        try:
            expiry, signature = value.split(".", 1)
            expected = hmac.new(
                cookie_key,
                f"{settings.role}:{expiry}".encode(),
                hashlib.sha256,
            ).hexdigest()
            return int(expiry) > time.time() and hmac.compare_digest(
                signature, expected
            )
        except (ValueError, AttributeError):
            return False

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.code)

    def credentials(request: Request) -> tuple[bool, bool]:
        """Return (authorised, cookie_only)."""

        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        is_device = bool(device_prefix and request.url.path.startswith(device_prefix))
        key = settings.token
        token_ok = (
            bool(key and bearer and hmac.compare_digest(bearer, key))
            if not is_device
            else False
        )
        if is_device:
            for device_key, identity in settings.device_registry():
                if device_key and bearer and hmac.compare_digest(bearer, device_key):
                    request.state.device = identity
                    token_ok = True
                    break
        cookie_ok = not is_device and valid_cookie(request.cookies.get(cookie_name))
        return token_ok or cookie_ok, cookie_ok and not token_ok

    @app.middleware("http")
    async def auth(request: Request, call_next):
        p = request.url.path
        if settings.role == "viewer":
            # The Visualizer gateway has no per-dataset authorisation of its own,
            # so it shares the Server's admin token. Browsers get a login form;
            # everything else (video ranges, Doctor JSON, data bridge) gets 401.
            if p in ("/api/login", "/api/logout", "/healthz"):
                response = await call_next(request)
            else:
                authorised, cookie_only = credentials(request)
                if not authorised:
                    wants_html = "text/html" in request.headers.get("accept", "")
                    if request.method in ("GET", "HEAD") and (
                        wants_html or p == "/login"
                    ):
                        return HTMLResponse(
                            VIEWER_LOGIN_PAGE,
                            status_code=200,
                            headers={
                                "Cache-Control": "no-store",
                                "X-Content-Type-Options": "nosniff",
                                "Referrer-Policy": "no-referrer",
                            },
                        )
                    return JSONResponse(
                        {"detail": "请先登录或提供有效凭据"}, status_code=401
                    )
                if (
                    cookie_only
                    and request.method not in ("GET", "HEAD", "OPTIONS")
                    and request.url.path != "/api/logout"
                ):
                    origin = request.headers.get("origin")
                    expected = f"{request.url.scheme}://{request.url.netloc}"
                    if origin != expected:
                        return JSONResponse(
                            {"detail": "Origin 校验失败"}, status_code=403
                        )
                response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-store"
            return response

        public = (
            p
            in (
                "/",
                "/healthz",
                "/api/login",
                "/assets/app.js",
                "/assets/annotations.js",
                "/assets/client-replay.js",
                "/assets/capture-service.js",
                "/assets/capture-telemetry.js",
                "/assets/preview-stream.js",
                "/assets/sonic-client.css",
                "/assets/sonic-client.js",
                "/assets/g1-onboard_collection.css",
                "/assets/g1-onboard_collection.js",
                "/assets/clipboard.js",
                "/assets/theme.js",
                "/assets/theme.css",
                "/assets/style.css",
                "/assets/report.js",
                "/assets/report.css",
            )
            or (
                settings.role == "server"
                and (p == "/report" or p.startswith("/report/"))
            )
            or (settings.role == "client" and p == "/replay")
        )
        if settings.role == "client":
            # Client is intentionally open on its trusted robot network. The
            # device token is still required separately when this Client calls
            # the Server upload API.
            response = await call_next(request)
        elif public and settings.role != "viewer":
            response = await call_next(request)
        else:
            authorised, cookie_only = credentials(request)
            if not authorised:
                return JSONResponse(
                    {"detail": "请先登录或提供有效凭据"}, status_code=401
                )
            if cookie_only and request.method not in ("GET", "HEAD", "OPTIONS"):
                origin = request.headers.get("origin")
                expected = f"{request.url.scheme}://{request.url.netloc}"
                if origin != expected:
                    return JSONResponse({"detail": "Origin 校验失败"}, status_code=403)
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if settings.role != "viewer":
            if p == "/capture-visualizer/" or (
                p == "/" and getattr(app.state, "native_client_page", False)
            ):
                # The trusted upstream visualizer keeps its original inline
                # bootstrap and styles. In native-client mode it is the document
                # itself, with Client APIs still protected by this middleware.
                response.headers["Content-Security-Policy"] = (
                    "default-src 'self'; img-src 'self' blob: data:; "
                    "script-src 'self' 'unsafe-inline'; "
                    "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
                )
            else:
                response.headers["Content-Security-Policy"] = (
                    "default-src 'self'; img-src 'self' blob: data:; "
                    "script-src 'self'; style-src 'self'; "
                    "frame-ancestors 'none'"
                )
        return response

    attempts = {}

    @app.post("/api/login")
    async def login(request: Request):
        if settings.role == "client":
            raise DomainError("Client 未启用密钥登录", 404)
        # Do not expose credentials in URLs, logs, localStorage, or HTML.
        from json import JSONDecodeError, loads

        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise DomainError("Origin 校验失败", 403)
        address = request.client.host if request.client else "unknown"
        recent = [t for t in attempts.get(address, []) if t > time.time() - 60]
        if len(recent) >= 10:
            raise DomainError("登录尝试过多，请稍后重试", 429)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise DomainError("请求过大", 413)
        try:
            body = loads(raw)
        except (JSONDecodeError, UnicodeError):
            raise DomainError("无效登录请求", 400)
        if len(attempts) > 10000:
            attempts.clear()
        attempts[address] = recent + [time.time()]
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("token"), str)
            or not hmac.compare_digest(body["token"].encode(), login_password.encode())
        ):
            raise DomainError("密钥不正确", 401)
        attempts.pop(address, None)
        expiry = str(int(time.time()) + 8 * 3600)
        sig = hmac.new(
            cookie_key,
            f"{settings.role}:{expiry}".encode(),
            hashlib.sha256,
        ).hexdigest()
        response = JSONResponse({"ok": True})
        response.set_cookie(
            cookie_name,
            expiry + "." + sig,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            max_age=8 * 3600,
        )
        return response

    @app.post("/api/logout")
    def logout():
        if settings.role == "client":
            raise DomainError("Client 未启用密钥登录", 404)
        response = JSONResponse({"ok": True})
        response.delete_cookie(cookie_name)
        return response
