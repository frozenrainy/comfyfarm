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
import time
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
SESSION_TTL = 30.0          # seconds a confirmed session is trusted before the router is asked again (D-35)


class Unavailable(Exception):
    """The router could not confirm a session: answer 503, never let the request through unconfirmed."""


class UiUnavailable(Exception):
    """The UI worker did not map the user to a ComfyUI user id: answer 502 with the reason."""

ROUTER_PREFIXES = ("/api/prompt", "/api/jobs", "/api/queue", "/api/history", "/api/interrupt", "/api/object_info",
                   "/api/farm/", "/api/admin/", "/api/health/",
                   "/comfyfarm/dashboard", "/prompt", "/queue", "/history", "/interrupt", "/object_info")
DASHBOARD = "/comfyfarm/dashboard/"
AFTER_LOGIN = ("/", DASHBOARD)           # the dashboard is the landing page; "/" (bare ComfyUI) only when asked for
# Same look as the dashboard; the theme the browser last used there (localStorage) before the first paint.
LOGIN_PAGE = """<!doctype html><html lang={lang}><head><meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>ComfyFarm</title>
<script>
if (window.top !== window) window.top.location.href = location.href;   // an expired session inside the Workflow tab
document.documentElement.dataset.theme = localStorage.getItem("comfyfarm.theme") || "auto";
</script>
<style>
:root {{ --bg:#f7f7f5; --panel:#fff; --line:#e3e3df; --text:#1f2328; --muted:#6a6f76; --accent:#0b5cad; --bad:#c62828; color-scheme:light }}
:root[data-theme=dark] {{ --bg:#15171a; --panel:#1d2024; --line:#33383f; --text:#e6e8eb; --muted:#9aa1aa; --accent:#6cb0ff; --bad:#ff6b6b; color-scheme:dark }}
@media (prefers-color-scheme: dark) {{ :root[data-theme=auto] {{ --bg:#15171a; --panel:#1d2024; --line:#33383f; --text:#e6e8eb; --muted:#9aa1aa; --accent:#6cb0ff; --bad:#ff6b6b; color-scheme:dark }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; background:var(--bg); color:var(--text);
        font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; word-break:keep-all }}
form {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:1.6rem 1.8rem; width:min(22rem,92vw);
        display:flex; flex-direction:column; gap:.7rem }}
h1 {{ font-size:1.15rem; margin:0 0 .3rem }}
input, select {{ font:inherit; color:var(--text); background:var(--panel); border:1px solid var(--line); border-radius:6px; padding:.5rem .6rem; width:100% }}
button {{ font:inherit; padding:.5rem; border:0; border-radius:6px; background:var(--accent); color:var(--panel); cursor:pointer; font-weight:600 }}
p {{ margin:0 }} .bad {{ color:var(--bad) }} .note {{ color:var(--muted) }}
</style></head><body>
{form}
</body></html>"""
LOGIN_FORM = """<form method=post action=/comfyfarm/login><h1>ComfyFarm</h1>{msg}<input type=hidden name=next value="{next}">
{user_field}<input name=password type=password placeholder="{password}" autocomplete=current-password><button>{login}</button></form>"""
# A fresh install (no administrator): the first one is made here with the setup code from the router's log.
SETUP_FORM = """<form method=post action=/comfyfarm/setup><h1>{title}</h1><p class=note>{note}</p>{msg}
<input name=code placeholder="{code}" autocomplete=off autofocus spellcheck=false>
<input name=username placeholder="{user}" autocomplete=username>
<input name=password type=password placeholder="{password}" autocomplete=new-password>
<input name=again type=password placeholder="{again}" autocomplete=new-password><button>{create}</button></form>"""
IDENTITY_HEADERS = {"x-comfyfarm-user", "x-comfyfarm-roles", "x-comfyfarm-gateway", "comfy-user",
                    "x-comfyfarm-gateway-login"}
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
               "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding",
               # the gateway does the cross-site check itself (same_origin_ok); upstream ComfyUI would compare the
               # browser's Origin (the gateway) with its own Host and refuse every proxied request with 403
               "origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user"}
HASHED_ASSET = re.compile(r"^/assets/.+[-.][A-Za-z0-9_]{8,}\.(js|css|woff2?|svg|png|webp)$")
COOKIE = "comfyfarm_session"
# The login page in the language the dashboard was last set to (cookie comfyfarm_lang), else the browser's.
LOGIN_TEXT = {
    "en": {"user": "User", "password": "Password", "login": "Log in", "failed": "Login failed",
           "down": "The farm is not answering — try again shortly", "pw": "Password changed. Log in again.",
           "setup": "Create the administrator", "code": "Setup code", "again": "Password again", "create": "Create and log in",
           "setup_note": "No administrator yet. The setup code is in the router's log "
                         "(docker compose logs router).",
           "s_code": "Wrong setup code", "s_differ": "The passwords differ", "s_short": "Password: at least 8 characters",
           "s_name": "User name: letters, digits and . _ @ -, starting with a letter or digit"},
    "ko": {"user": "사용자", "password": "비밀번호", "login": "로그인", "failed": "로그인하지 못했습니다",
           "down": "팜이 응답하지 않습니다 — 잠시 뒤 다시 시도하세요", "pw": "비밀번호를 바꿨습니다. 다시 로그인하세요.",
           "setup": "관리자 만들기", "code": "설정 코드", "again": "비밀번호 확인", "create": "만들고 로그인",
           "setup_note": "아직 관리자가 없습니다. 설정 코드는 라우터 로그에 있습니다 (docker compose logs router).",
           "s_code": "설정 코드가 맞지 않습니다", "s_differ": "두 비밀번호가 다릅니다", "s_short": "비밀번호: 8자 이상",
           "s_name": "사용자 이름: 영문·숫자·. _ @ -, 영문이나 숫자로 시작"},
}


def login_lang(request) -> str:
    pref = request.cookies.get("comfyfarm_lang", "auto")
    if pref in LOGIN_TEXT:
        return pref
    for part in request.headers.get("accept-language", "").split(","):
        base = part.split(";")[0].strip().lower().split("-")[0]
        if base in LOGIN_TEXT:
            return base
    return "en"
LOOPBACK = ("127.0.0.1", "::1", "localhost")


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
    session_cache: dict[tuple, tuple[float, frozenset]] = {}

    async def confirm(user: str, epoch: int | None, roles: frozenset) -> frozenset | None:
        """Current roles of a live account, or None if it is disabled, deleted or its sessions were ended (password
        change). Asked from the router and cached SESSION_TTL seconds, so a revocation takes effect within that time
        instead of when the cookie expires (D-35). Raises Unavailable when the router cannot answer."""
        key, now = (user, epoch, roles), time.monotonic()
        hit = session_cache.get(key)
        if hit and now - hit[0] < SESSION_TTL:
            return hit[1]
        try:
            r = await http.post(f"{s.router_url}/api/internal/session", headers={"x-comfyfarm-gateway-login": s.secret},
                                json={"user": user, "epoch": epoch, "roles": sorted(roles)}, timeout=5)
        except httpx.HTTPError as e:
            raise Unavailable(type(e).__name__)
        if r.status_code == 200:
            if len(session_cache) > 10000:
                session_cache.clear()
            got = frozenset(r.json()["roles"])
            session_cache[key] = (now, got)
            return got
        if r.status_code == 401:
            session_cache.pop(key, None)
            return None
        raise Unavailable(f"router answered {r.status_code}")

    async def identify(request) -> Identity | None:
        if s.provider == "dev":
            host = request.client.host if request.client else ""
            return Identity(s.dev_user, frozenset({"admin"})) if host in ("127.0.0.1", "::1") else None
        if s.provider == "trusted-header":
            peer = request.client.host if request.client else ""
            if peer not in s.trusted_proxies:
                return None                                   # only the authenticating proxy may assert identity
            user = request.headers.get(s.user_header)
            roles = frozenset(r.strip() for r in request.headers.get(s.roles_header, "").split(",") if r.strip())
            if not user:
                return None
            got = await confirm(user, None, roles)            # creates the account row on first sight
            return Identity(user, got) if got is not None else None
        ident = read_session(request.cookies.get(COOKIE), s.session_key)
        if ident is None or ident.epoch is None:
            return None
        got = await confirm(ident.user, ident.epoch, frozenset())
        return Identity(ident.user, got, ident.epoch) if got is not None else None

    def router_headers(request, ident: Identity) -> dict:
        h = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS | IDENTITY_HEADERS}
        h.update({"x-comfyfarm-user": ident.user, "x-comfyfarm-roles": ",".join(sorted(ident.roles)),
                  "x-comfyfarm-gateway": s.secret})
        return h

    ui_found = {"url": None, "at": 0.0}

    async def ui_base() -> str:
        """The UI worker: fixed by --ui, or with --ui auto the one registered in the router (System → Workers → Set up
        a worker), re-asked every SESSION_TTL. ComfyUI user ids belong to one UI worker: a new one clears the mapping."""
        if s.ui_url != "auto":
            return s.ui_url
        now = time.monotonic()
        if ui_found["url"] and now - ui_found["at"] < SESSION_TTL:
            return ui_found["url"]
        try:
            r = await http.get(f"{s.router_url}/api/internal/ui-worker", headers={"x-comfyfarm-gateway-login": s.secret}, timeout=5)
        except httpx.HTTPError as e:
            if ui_found["url"]:
                return ui_found["url"]                       # the router is down: keep the last known one
            raise UiUnavailable(f"the router does not answer, so the UI worker is unknown ({type(e).__name__})")
        if r.status_code != 200:
            raise UiUnavailable("no UI worker is set up yet — System → Workers → Set up a worker → UI worker")
        url = r.json()["url"].rstrip("/")
        if url != ui_found["url"]:
            s.comfy_users.clear()
        ui_found.update(url=url, at=now)
        return url

    async def comfy_user_id(ident: Identity) -> str:
        """Map the identity to a stock multi-user id on the UI worker (created once). Stock ComfyUI generates ids
        ('name_<uuid>'), so the mapping is looked up by username."""
        ui = await ui_base()                             # first: a different UI worker clears the mapping
        if ident.user in s.comfy_users:
            return s.comfy_users[ident.user]
        async with comfy_user_lock:
            if ident.user in s.comfy_users:
                return s.comfy_users[ident.user]
            try:
                r = await http.get(f"{ui}/api/users")
                if r.status_code == 200:
                    users = r.json().get("users") or {}
                    uid = next((k for k, v in users.items() if v == ident.user), None)
                    if uid is None:
                        r = await http.post(f"{ui}/api/users", json={"username": ident.user})
                        uid = r.json() if r.status_code == 200 else None
            except (httpx.HTTPError, ValueError, AttributeError) as e:
                raise UiUnavailable(f"the UI worker at {ui} is unreachable or not ComfyUI ({type(e).__name__})")
            if r.status_code == 403:
                raise UiUnavailable(f"the UI worker at {ui} refused the gateway (403) — if it runs with "
                                    "COMFYFARM_ALLOW, the gateway's address must be in it")
            if r.status_code != 200 or not isinstance(uid, str):
                raise UiUnavailable(f"the UI worker at {ui} answered {r.status_code} to /api/users — "
                                    "is it started with --multi-user?")
            s.comfy_users[ident.user] = uid
            return uid

    async def proxy(request: Request, base: str, headers: dict, cache_immutable: bool = False,
                    miss: int | None = None) -> Response | None:
        """`miss`: an upstream status that means "try elsewhere" — the response is dropped and None returned.
        The path goes upstream as the browser sent it (raw): routing decided on the decoded path, and that path holds
        no dot segments (plain_path), so the upstream cannot resolve it to anything else."""
        raw = (request.scope.get("raw_path") or request.url.path.encode()).decode("latin-1")
        url = f"{base}{raw}" + (f"?{request.url.query}" if request.url.query else "")
        body = request.stream() if request.method not in ("GET", "HEAD") else None
        req = http.build_request(request.method, url, headers=headers, content=body)
        try:
            up = await http.send(req, stream=True)
        except httpx.HTTPError as e:
            return JSONResponse({"error": f"upstream unavailable: {type(e).__name__}"}, status_code=502)
        if miss is not None and up.status_code == miss:
            await up.aclose()
            return None
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
    def safe_next(v) -> str:
        return v if v in AFTER_LOGIN else DASHBOARD   # fixed list: a free-form target would be an open redirect

    async def login_users() -> list[str] | None:
        """User names for the login form's drop-down, if an admin enabled it (System → Settings); None otherwise or
        when the router cannot say — the form then falls back to a text field."""
        try:
            r = await http.get(f"{s.router_url}/api/internal/login-options", headers={"x-comfyfarm-gateway-login": s.secret},
                               timeout=3)
            return r.json().get("users") if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    async def first_admin_needed() -> bool | None:
        """True: no administrator yet; False: there is one; None: the router did not answer (say so — right after
        `docker compose up` the router may still be starting, and a silent ordinary login form misleads)."""
        try:
            r = await http.get(f"{s.router_url}/api/internal/first-admin", headers={"x-comfyfarm-gateway-login": s.secret},
                               timeout=3)
            return r.status_code == 200 and r.json().get("needed") is True
        except (httpx.HTTPError, ValueError):
            return None

    async def login_page(request: Request):
        lang = login_lang(request)
        tx = LOGIN_TEXT[lang]
        needed = await first_admin_needed() if s.provider == "local" else False
        if needed:
            err = request.query_params.get("s")
            msg = f'<p class=bad>{tx["s_" + err]}</p>' if err in ("code", "differ", "short", "name") else ""
            if request.query_params.get("e") == "2":
                msg = f'<p class=bad>{tx["down"]}</p>'
            return HTMLResponse(LOGIN_PAGE.format(lang=lang, form=SETUP_FORM.format(
                title=tx["setup"], note=html.escape(tx["setup_note"]), msg=msg, code=tx["code"], user=tx["user"],
                password=tx["password"], again=tx["again"], create=tx["create"])))
        err = request.query_params.get("e")
        note = tx["pw"] if request.query_params.get("m") == "pw" else ""
        nxt = safe_next(request.query_params.get("next"))
        users = await login_users() if s.provider == "local" else None
        if users:
            options = "".join(f"<option>{html.escape(u)}</option>" for u in users)
            user_field = f'<select name=username autofocus><option value="" disabled selected>{tx["user"]}</option>{options}</select>'
        else:
            user_field = f'<input name=username placeholder="{tx["user"]}" autocomplete=username autofocus>'
        msg = (f'<p class=bad>{tx["down"] if err == "2" or needed is None else tx["failed"]}</p>' if err or needed is None else "") \
            + (f'<p class=note>{html.escape(note)}</p>' if note else "")
        return HTMLResponse(LOGIN_PAGE.format(lang=lang, form=LOGIN_FORM.format(
            next=html.escape(nxt), user_field=user_field, msg=msg, password=tx["password"], login=tx["login"])))

    async def setup(request: Request):
        """The first administrator (fresh install): the router checks the setup code; on success the new administrator
        is logged in like after a password login."""
        if not same_origin_ok(request) or s.provider != "local":
            return Response(status_code=403)
        form = await request.form()
        if form.get("password", "") != form.get("again", ""):
            return RedirectResponse("/comfyfarm/login?s=differ", status_code=303)
        try:
            r = await http.post(f"{s.router_url}/api/internal/first-admin", headers={"x-comfyfarm-gateway-login": s.secret},
                                json={k: form.get(k, "") for k in ("code", "username", "password")})
        except httpx.HTTPError:
            return RedirectResponse("/comfyfarm/login?e=2", status_code=303)
        if r.status_code == 403:
            return RedirectResponse("/comfyfarm/login?s=code", status_code=303)
        if r.status_code == 400:
            detail = str((r.json() if r.headers.get("content-type", "").startswith("application/json") else {}).get("detail", ""))
            return RedirectResponse(f"/comfyfarm/login?s={'short' if detail.startswith('password') else 'name'}", status_code=303)
        if r.status_code != 200:
            return RedirectResponse("/comfyfarm/login", status_code=303)      # an administrator exists: the login form
        d = r.json()
        resp = RedirectResponse(DASHBOARD, status_code=303)
        resp.set_cookie(COOKIE, sign_session(Identity(d["user"], frozenset(), d["epoch"]), s.session_key),
                        httponly=True, samesite="lax", secure=s.secure_cookie, max_age=7 * 86400)
        return resp

    async def login(request: Request):
        if not same_origin_ok(request):
            return Response(status_code=403)
        form = await request.form()
        nxt = safe_next(form.get("next"))
        try:
            r = await http.post(f"{s.router_url}/api/internal/login", headers={"x-comfyfarm-gateway-login": s.secret},
                                json={"username": form.get("username", ""), "password": form.get("password", "")})
        except httpx.HTTPError:
            return RedirectResponse(f"/comfyfarm/login?next={nxt}&e=2", status_code=303)
        if r.status_code >= 500:
            return RedirectResponse(f"/comfyfarm/login?next={nxt}&e=2", status_code=303)
        if r.status_code != 200:
            return RedirectResponse(f"/comfyfarm/login?next={nxt}&e=1", status_code=303)
        d = r.json()
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(COOKIE, sign_session(Identity(d["user"], frozenset(), d["epoch"]), s.session_key),
                        httponly=True, samesite="lax", secure=s.secure_cookie, max_age=7 * 86400)
        return resp

    async def logout(request: Request):
        resp = RedirectResponse("/comfyfarm/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    def same_origin_ok(request) -> bool:
        """CSRF guard owned by the gateway (stock ComfyUI's Host/Origin check cannot work behind a proxy):
        refuse cross-site fetches and any Origin that is not this gateway. A cross-site top-level page load (a link
        from an intranet page, the return from a single sign-on) is let through: GETs change nothing."""
        if request.headers.get("sec-fetch-site") == "cross-site":
            return (getattr(request, "method", None) in ("GET", "HEAD")
                    and request.headers.get("sec-fetch-mode") == "navigate")
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            return urlparse(origin).netloc.lower() == request.headers.get("host", "").lower()
        return True

    # ---- everything else ---------------------------------------------------------------------------------------
    def plain_path(path: str) -> bool:
        """Routing looks at the path; a `..` (also sent as %2e%2e) or `//` would let the upstream or the HTTP client
        resolve it to a different one — /x/../prompt would reach the UI worker's own queue."""
        return "//" not in path and not any(seg in (".", "..") for seg in path.split("/")) and "\\" not in path

    async def handle(request: Request):
        if not same_origin_ok(request):
            return Response(status_code=403)
        if not plain_path(request.url.path):
            return JSONResponse({"error": "bad path"}, status_code=400)
        try:
            ident = await identify(request)
        except Unavailable as e:
            return JSONResponse({"error": f"router unavailable ({e})"}, status_code=503, headers={"Retry-After": "2"})
        path = request.url.path
        if ident is None:
            if request.method == "GET" and s.provider == "local":
                if path in ("/", "/index.html"):
                    return RedirectResponse("/comfyfarm/login", status_code=303)
                if path.startswith("/comfyfarm/dashboard"):
                    return RedirectResponse(f"/comfyfarm/login?next={DASHBOARD}", status_code=303)
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        if path.startswith(ROUTER_PREFIXES):
            return await proxy(request, s.router_url, router_headers(request, ident))
        base_headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS | IDENTITY_HEADERS}
        if path in ("/api/view", "/view") and any(len(request.query_params.getlist(k)) > 1
                                                  for k in ("filename", "subfolder", "type")):
            # the router's owner check and the worker would read different copies of a repeated parameter
            return JSONResponse({"error": "repeated file parameter"}, status_code=400)
        if path in ("/api/view", "/view") and request.query_params.get("type") in ("temp", "output"):
            # temp files exist only on the executing worker; saved outputs normally sit on shared storage (01) and
            # are served by the UI worker — when it does not have one, the executing worker is asked (a site without
            # shared storage for outputs still works, slower)
            kind = request.query_params.get("type")
            if kind == "output":
                try:
                    base_headers["comfy-user"] = await comfy_user_id(ident)
                    resp = await proxy(request, await ui_base(), base_headers, miss=404)
                except UiUnavailable as e:                    # still try the executing worker below
                    log.error("%s", e)
                    resp = None
                if resp is not None:
                    return resp
            q = {k: request.query_params.get(k, "") for k in ("filename", "subfolder", "type")}
            try:
                loc = await http.get(f"{s.router_url}/api/farm/locate?{urlencode(q)}", headers=router_headers(request, ident))
            except httpx.HTTPError as e:
                return JSONResponse({"error": f"router unavailable ({type(e).__name__})"}, status_code=503,
                                    headers={"Retry-After": "2"})
            if loc.status_code == 200:
                return await proxy(request, loc.json()["base_url"], base_headers)
            if kind == "output":
                return JSONResponse({"error": "not found"}, status_code=404)
        if path in ("/api/users", "/users") and request.method == "GET":
            # the stock frontend shows a user picker in multi-user mode; the gateway already knows who this is
            return JSONResponse({"storage": "server", "migrated": True})
        try:
            ui = await ui_base()
            base_headers["comfy-user"] = await comfy_user_id(ident)
        except UiUnavailable as e:
            log.error("%s", e)
            return JSONResponse({"error": str(e)}, status_code=502)
        return await proxy(request, ui, base_headers, cache_immutable=bool(HASHED_ASSET.match(path)))

    async def ws_endpoint(ws: WebSocket):
        if not same_origin_ok(ws) or not plain_path(ws.url.path):
            await ws.close(code=4403)
            return
        try:
            ident = await identify(ws)
        except Unavailable:
            await ws.close(code=1013)                         # try again later
            return
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
                async def session_alive():
                    """The session is re-confirmed while the socket is open: a disabled account or changed password
                    ends it within SESSION_TTL, as for requests (D-35). A router outage does not — the upstream
                    socket fails on its own then."""
                    while True:
                        await asyncio.sleep(SESSION_TTL)
                        with contextlib.suppress(Unavailable):
                            if await identify(ws) is None:
                                return

                tasks = [asyncio.create_task(down()), asyncio.create_task(upward()), asyncio.create_task(session_alive())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                code = 4401 if tasks[2] in done else (up.close_code or 1000)
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
        Route("/comfyfarm/setup", setup, methods=["POST"]),
        Route("/comfyfarm/logout", logout, methods=["GET", "POST"]),
        WebSocketRoute("/ws", ws_endpoint),
        Route("/{path:path}", handle, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]),
    ], lifespan=lifespan)


def main(argv=None):
    import os
    import uvicorn
    from .logs import setup
    ap = argparse.ArgumentParser(prog="comfyfarm-gateway",
                                 description="Browser entry point: login, ComfyUI frontend, routing to the router and workers. "
                                             "Needs COMFYFARM_GATEWAY_SECRET (same as the router) and COMFYFARM_SESSION_KEY "
                                             "(see `comfyfarm secrets`), or each as a file: NAME_FILE=<path>.")
    ap.add_argument("--router", required=True, help="router URL, e.g. http://127.0.0.1:8188")
    ap.add_argument("--ui", default="auto", help="UI worker URL (a stock ComfyUI with the comfyfarm_ui extension), or "
                                                 "auto: the UI worker set up in the dashboard (default)")
    ap.add_argument("--provider", default="local", choices=["local", "trusted-header", "dev"])
    ap.add_argument("--trusted-proxy", action="append", default=[],
                    help="trusted-header: address of the authenticating reverse proxy (repeatable)")
    ap.add_argument("--secure-cookie", action="store_true", help="mark the session cookie Secure (when served over TLS)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--log-dir")
    a = ap.parse_args(argv)
    from .envfile import load_secret_files
    load_secret_files()                                  # COMFYFARM_GATEWAY_SECRET_FILE / COMFYFARM_SESSION_KEY_FILE
    secret = os.environ.get("COMFYFARM_GATEWAY_SECRET") or ap.error("COMFYFARM_GATEWAY_SECRET is not set")
    key = (os.environ.get("COMFYFARM_SESSION_KEY") or ap.error("COMFYFARM_SESSION_KEY is not set")).encode()
    if len(key) < 32:
        ap.error("COMFYFARM_SESSION_KEY must be at least 32 characters (comfyfarm secrets)")
    if a.provider == "trusted-header" and not a.trusted_proxy:
        ap.error("--provider trusted-header needs --trusted-proxy <address>")
    if a.provider == "dev" and a.host not in LOOPBACK:
        ap.error("--provider dev makes every local connection an administrator: it needs --host 127.0.0.1")
    setup(a.log_dir, name="gateway")
    app = create_gateway(GatewaySettings(a.router.rstrip("/"), a.ui.rstrip("/"), secret, key, provider=a.provider,
                                         trusted_proxies=tuple(a.trusted_proxy), secure_cookie=a.secure_cookie))
    # proxy_headers=False: the peer address is the connection's own — uvicorn would otherwise replace it with
    # X-Forwarded-For, which the browser can write, and trusted-header decides on that address
    uvicorn.run(app, host=a.host, port=a.port, log_config=None, proxy_headers=False)


if __name__ == "__main__":
    main()
