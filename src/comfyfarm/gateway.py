"""Gateway: the single browser entry point (docs/design/08-gateway-and-identity.md).

  /ws, /api/prompt, /api/jobs*, /api/queue*, /api/history*, /api/interrupt, /api/object_info*, /api/farm/*, /api/admin/*
                                      → router   (identity forwarded with the shared secret)
  /api/view?type=temp…                → the GPU worker that produced the file (asked from the router)
  everything else (frontend, user data, settings, assets)
                                      → UI worker (Comfy-User set by the gateway, never by the browser)

Run:  python -m comfyfarm.gateway --router http://127.0.0.1:8188 --ui http://127.0.0.1:18188 --secret … --session-key …
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .identity import Identity, read_session, sign_session

log = logging.getLogger(__name__)

ROUTER_PREFIXES = ("/api/prompt", "/api/jobs", "/api/queue", "/api/history", "/api/interrupt", "/api/object_info",
                   "/api/farm/", "/api/admin/", "/api/health/", "/prompt", "/queue", "/history", "/interrupt", "/object_info")
IDENTITY_HEADERS = {"x-comfyfarm-user", "x-comfyfarm-roles", "x-comfyfarm-gateway", "comfy-user",
                    "x-comfyfarm-gateway-login"}
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
               "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}
HASHED_ASSET = re.compile(r"^/assets/.+[-.][A-Za-z0-9_]{8,}\.(js|css|woff2?|svg|png|webp)$")
COOKIE = "comfyfarm_session"


@dataclass
class GatewaySettings:
    router_url: str
    ui_url: str
    secret: str
    session_key: bytes
    provider: str = "local"                 # local | trusted-header | dev
    trusted_proxies: tuple[str, ...] = ()   # trusted-header: peers allowed to assert identity
    user_header: str = "x-forwarded-user"
    roles_header: str = "x-forwarded-groups"
    dev_user: str = "dev"
    secure_cookie: bool = False
    comfy_users: dict[str, str] = field(default_factory=dict)   # identity → UI worker user id (cache)


def create_gateway(s: GatewaySettings) -> Starlette:
    http = httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=300, write=60, pool=10))
    comfy_user_lock = asyncio.Lock()

    # ---- identity ----------------------------------------------------------------------------------------------
    def identify(request) -> Identity | None:
        if s.provider == "dev":
            host = request.client.host if request.client else ""
            return Identity(s.dev_user, frozenset({"admin"})) if host in ("127.0.0.1", "::1") else None
        if s.provider == "trusted-header":
            peer = request.client.host if request.client else ""
            if peer not in s.trusted_proxies:
                return None                                   # only the authenticating proxy may assert identity
            user = request.headers.get(s.user_header)
            roles = frozenset(r.strip() for r in request.headers.get(s.roles_header, "").split(",") if r.strip())
            return Identity(user, roles) if user else None
        return read_session(request.cookies.get(COOKIE), s.session_key)

    def router_headers(request, ident: Identity) -> dict:
        h = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS | IDENTITY_HEADERS}
        h.update({"x-comfyfarm-user": ident.user, "x-comfyfarm-roles": ",".join(sorted(ident.roles)),
                  "x-comfyfarm-gateway": s.secret})
        return h

    async def comfy_user_id(ident: Identity) -> str:
        """Map the identity to a stock multi-user id on the UI worker (created once). Stock ComfyUI generates ids
        ('name_<uuid>'), so the mapping is looked up by username."""
        if ident.user in s.comfy_users:
            return s.comfy_users[ident.user]
        async with comfy_user_lock:
            if ident.user in s.comfy_users:
                return s.comfy_users[ident.user]
            users = (await http.get(f"{s.ui_url}/api/users")).json().get("users") or {}
            uid = next((k for k, v in users.items() if v == ident.user), None)
            if uid is None:
                r = await http.post(f"{s.ui_url}/api/users", json={"username": ident.user})
                uid = r.json() if r.status_code == 200 else None
            if uid is None:
                raise RuntimeError(f"cannot create UI user for {ident.user!r}")
            s.comfy_users[ident.user] = uid
            return uid

    async def proxy(request: Request, base: str, headers: dict, cache_immutable: bool = False) -> Response:
        url = f"{base}{request.url.path}" + (f"?{request.url.query}" if request.url.query else "")
        body = request.stream() if request.method not in ("GET", "HEAD") else None
        req = http.build_request(request.method, url, headers=headers, content=body)
        try:
            up = await http.send(req, stream=True)
        except httpx.HTTPError as e:
            return JSONResponse({"error": f"upstream unavailable: {type(e).__name__}"}, status_code=502)
        out_headers = {k: v for k, v in up.headers.items() if k.lower() not in HOP_HEADERS | {"content-encoding"}}
        if cache_immutable and up.status_code == 200:
            out_headers["cache-control"] = "public, max-age=31536000, immutable"    # stock sends no-store

        async def body_iter():
            try:
                async for chunk in up.aiter_raw():
                    yield chunk
            finally:                     # released here, not in a background task: a disconnecting client skips those
                await up.aclose()
        return StreamingResponse(body_iter(), status_code=up.status_code, headers=out_headers)

    # ---- login -------------------------------------------------------------------------------------------------
    async def login_page(request: Request):
        err = request.query_params.get("e")
        return HTMLResponse(f"""<!doctype html><meta charset=utf-8><title>ComfyFarm</title>
<style>body{{font-family:system-ui;max-width:22rem;margin:12vh auto;color:#222}}input{{width:100%;padding:.5rem;margin:.3rem 0}}
button{{padding:.5rem 1rem}}</style><h2>ComfyFarm</h2>{'<p style=color:#b00>Login failed</p>' if err else ''}
<form method=post action=/comfyfarm/login><input name=username placeholder=Username autofocus>
<input name=password type=password placeholder=Password><button>Log in</button></form>""")

    async def login(request: Request):
        form = await request.form()
        r = await http.post(f"{s.router_url}/api/internal/login", headers={"x-comfyfarm-gateway-login": s.secret},
                            json={"username": form.get("username", ""), "password": form.get("password", "")})
        if r.status_code != 200:
            return RedirectResponse("/comfyfarm/login?e=1", status_code=303)
        d = r.json()
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, sign_session(Identity(d["user"], frozenset(d["roles"])), s.session_key),
                        httponly=True, samesite="lax", secure=s.secure_cookie, max_age=7 * 86400)
        return resp

    async def logout(request: Request):
        resp = RedirectResponse("/comfyfarm/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    # ---- everything else ---------------------------------------------------------------------------------------
    async def handle(request: Request):
        ident = identify(request)
        path = request.url.path
        if ident is None:
            if request.method == "GET" and path in ("/", "/index.html") and s.provider == "local":
                return RedirectResponse("/comfyfarm/login", status_code=303)
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        if path.startswith(ROUTER_PREFIXES):
            return await proxy(request, s.router_url, router_headers(request, ident))
        base_headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS | IDENTITY_HEADERS}
        if path in ("/api/view", "/view") and request.query_params.get("type") == "temp":
            q = {k: request.query_params.get(k, "") for k in ("filename", "subfolder", "type")}
            loc = await http.get(f"{s.router_url}/api/farm/locate?{urlencode(q)}", headers=router_headers(request, ident))
            if loc.status_code == 200:
                return await proxy(request, loc.json()["base_url"], base_headers)
        if path in ("/api/users", "/users") and request.method == "GET":
            # the stock frontend shows a user picker in multi-user mode; the gateway already knows who this is
            return JSONResponse({"storage": "server", "migrated": True})
        base_headers["comfy-user"] = await comfy_user_id(ident)
        return await proxy(request, s.ui_url, base_headers, cache_immutable=bool(HASHED_ASSET.match(path)))

    async def ws_endpoint(ws: WebSocket):
        ident = identify(ws)
        if ident is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        q = ws.url.query
        upstream_url = s.router_url.replace("http", "ws", 1) + "/ws" + (f"?{q}" if q else "")
        hdrs = {"x-comfyfarm-user": ident.user, "x-comfyfarm-roles": ",".join(sorted(ident.roles)),
                "x-comfyfarm-gateway": s.secret}
        try:
            async with websockets.connect(upstream_url, additional_headers=hdrs, max_size=None) as up:
                async def down():
                    async for m in up:
                        if isinstance(m, str):
                            await ws.send_text(m)
                        else:
                            await ws.send_bytes(m)

                async def upward():
                    while True:
                        m = await ws.receive()
                        if m["type"] == "websocket.disconnect":
                            return
                        if m.get("text") is not None:
                            await up.send(m["text"])
                        elif m.get("bytes") is not None:
                            await up.send(m["bytes"])
                tasks = [asyncio.create_task(down()), asyncio.create_task(upward())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                code = up.close_code or 1000
        except (websockets.ConnectionClosed, WebSocketDisconnect, OSError):
            code = 1011
        with contextlib.suppress(Exception):
            await ws.close(code=code if code in (1000, 1013, 4401, 4403) else 1011)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        yield
        await http.aclose()

    return Starlette(routes=[
        Route("/comfyfarm/login", login_page, methods=["GET"]),
        Route("/comfyfarm/login", login, methods=["POST"]),
        Route("/comfyfarm/logout", logout, methods=["GET", "POST"]),
        WebSocketRoute("/ws", ws_endpoint),
        Route("/{path:path}", handle, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]),
    ], lifespan=lifespan)


def main():
    import os
    import uvicorn
    ap = argparse.ArgumentParser(prog="comfyfarm-gateway")
    ap.add_argument("--router", required=True)
    ap.add_argument("--ui", required=True)
    ap.add_argument("--provider", default="local", choices=["local", "trusted-header", "dev"])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    secret = os.environ.get("COMFYFARM_GATEWAY_SECRET") or ap.error("COMFYFARM_GATEWAY_SECRET is not set")
    key = (os.environ.get("COMFYFARM_SESSION_KEY") or ap.error("COMFYFARM_SESSION_KEY is not set")).encode()
    app = create_gateway(GatewaySettings(a.router.rstrip("/"), a.ui.rstrip("/"), secret, key, provider=a.provider))
    uvicorn.run(app, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
