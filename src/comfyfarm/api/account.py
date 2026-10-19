"""Own account (settings page) and the gateway's internal identity calls (docs/design/08 'Accounts and permissions')."""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from ..core import access
from ..identity import hash_password, verify_password
from .context import Ctx

THEMES = ("light", "dark", "auto")
MIN_PASSWORD = 8
LANGS = ("auto", "en", "ko")          # auto: the browser's language


class PrefsIn(BaseModel):
    theme: str | None = None
    lang: str | None = None


class PasswordIn(BaseModel):
    current: str
    new: str


def password_problem(pw: str) -> str | None:
    if len(pw) < MIN_PASSWORD:
        return f"at least {MIN_PASSWORD} characters"
    return None


def make_router(ctx: Ctx) -> APIRouter:
    api = APIRouter()
    db = ctx.router.db

    def gateway_only(request: Request):
        s = ctx.settings.gateway_secret
        if not s or not hmac.compare_digest(request.headers.get("x-comfyfarm-gateway-login", "").encode("utf-8", "replace"), s.encode()):
            raise HTTPException(403, "gateway only")

    # ---- gateway ------------------------------------------------------------------------------------------------
    @api.get("/api/internal/ui-worker")
    async def ui_worker(request: Request):
        """The gateway with UI_WORKER_URL=auto asks where the UI worker is: the enabled UI worker, online ones first."""
        gateway_only(request)
        async with db.conn() as c:
            url = await c.fetchval("SELECT base_url FROM workers WHERE kind = 'ui' AND enabled AND deleted_at IS NULL "
                                   "ORDER BY state = 'online' DESC, id LIMIT 1")
        if url is None:
            raise HTTPException(404, "no UI worker: System → Workers → Set up a worker (UI worker)")
        return {"url": url}

    @api.post("/api/internal/login")
    async def internal_login(request: Request):
        """Local identity provider: check a password, hand out the session epoch the cookie will carry."""
        gateway_only(request)
        body = await request.json()
        async with db.conn() as c:
            u = await c.fetchrow("SELECT username, password_hash, session_epoch FROM users "
                                 "WHERE username = $1 AND disabled_at IS NULL", body.get("username", ""))
        if u is None or not u["password_hash"] or not verify_password(body.get("password", ""), u["password_hash"]):
            raise HTTPException(401, "bad credentials")
        return {"user": u["username"], "epoch": u["session_epoch"]}

    # ---- first administrator (a fresh install, easy deployment) -------------------------------------------------------
    @api.get("/api/internal/first-admin")
    async def first_admin_needed(request: Request):
        """Does the login page offer to create the first administrator? Only while the router holds a setup code and
        no administrator exists (one made with `comfyfarm add-user` meanwhile ends it)."""
        gateway_only(request)
        if ctx.router.setup_code is not None:
            async with db.conn() as c:
                if await c.fetchval("SELECT count(*) FROM users WHERE 'admin' = ANY(roles) AND disabled_at IS NULL"):
                    ctx.router.setup_code = None
        return {"needed": ctx.router.setup_code is not None}

    @api.post("/api/internal/first-admin")
    async def first_admin(request: Request):
        """Create the first administrator with the setup code from the router's log; the code ends with it."""
        gateway_only(request)
        from ..rollout.engine import audit
        from .admin import USERNAME
        body = await request.json()
        code = ctx.router.setup_code
        if code is None:
            raise HTTPException(409, "an administrator exists — log in")
        given = str(body.get("code", "")).strip().upper().replace(" ", "")
        if not hmac.compare_digest(given.encode(), code.encode()):
            raise HTTPException(403, "wrong setup code")
        name, password = str(body.get("username", "")), str(body.get("password", ""))
        if not USERNAME.match(name):
            raise HTTPException(400, "user name: letters, digits and . _ @ - (max 64), starting with a letter or digit")
        if problem := password_problem(password):
            raise HTTPException(400, f"password: {problem}")
        async with db.conn() as c:
            async with c.transaction():
                await c.execute("SELECT pg_advisory_xact_lock(hashtext('comfyfarm first administrator'))")
                if await c.fetchval("SELECT count(*) FROM users WHERE 'admin' = ANY(roles) AND disabled_at IS NULL"):
                    ctx.router.setup_code = None
                    raise HTTPException(409, "an administrator exists — log in")
                if await c.fetchval("SELECT 1 FROM users WHERE username = $1", name):
                    raise HTTPException(409, "that user name is taken")
                epoch = await c.fetchval("INSERT INTO users(username, password_hash, roles) VALUES ($1, $2, '{admin}') "
                                         "RETURNING session_epoch", name, hash_password(password))
                await audit(c, name, "first administrator created", name)
        ctx.router.setup_code = None
        return {"user": name, "epoch": epoch}

    @api.get("/api/internal/login-options")
    async def login_options(request: Request):
        """For the gateway's login form: the user names, only if an admin enabled the drop-down."""
        gateway_only(request)
        from .admin import get_setting
        async with db.conn() as c:
            if not await get_setting(c, "login_user_list"):
                return {"users": None}
            rows = await c.fetch("SELECT username FROM users WHERE disabled_at IS NULL AND password_hash IS NOT NULL "
                                 "ORDER BY lower(username)")
        return {"users": [r["username"] for r in rows]}

    @api.post("/api/internal/session")
    async def internal_session(request: Request):
        """Is this session still good, and with which roles? Asked by the gateway at most every SESSION_TTL per
        session (D-35). `epoch` null = an external provider vouched for the user: the account row is created on
        first sight and the provider's roles add to the stored ones."""
        gateway_only(request)
        body = await request.json()
        user, epoch, given = body.get("user") or "", body.get("epoch"), set(body.get("roles") or [])
        async with db.conn() as c:
            if epoch is None:
                await c.execute("INSERT INTO users(username) VALUES ($1) ON CONFLICT DO NOTHING", user)
            u = await c.fetchrow("SELECT roles, disabled_at, session_epoch FROM users WHERE username = $1", user)
        if u is None or u["disabled_at"] is not None or (epoch is not None and epoch != u["session_epoch"]):
            raise HTTPException(401, "session ended")
        return {"roles": sorted(set(u["roles"]) | (given if epoch is None else set()))}

    # ---- own account ----------------------------------------------------------------------------------------------
    @api.get("/api/farm/me")
    async def me(x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.identity(x_comfyfarm_user)
        async with db.conn() as c:
            row = await access.user_row(c, who)
            allowed = await ctx.allowed(c, who)
            groups = await c.fetch("SELECT id, name FROM worker_groups WHERE $1::bigint[] IS NULL OR id = ANY($1) ORDER BY name",
                                   allowed)
        prefs = dict(row["prefs"]) if row else {}
        return {"user": who, "admin": ctx.is_admin(who), "local": bool(row and row["local"]),
                "groups": [{"id": g["id"], "name": g["name"]} for g in groups], "all_groups": allowed is None,
                "prefs": {"theme": prefs.get("theme", "auto"), "lang": prefs.get("lang", "auto")}}

    @api.put("/api/farm/me/prefs")
    async def set_prefs(body: PrefsIn, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.identity(x_comfyfarm_user)
        patch = {}
        if body.theme is not None:
            if body.theme not in THEMES:
                raise HTTPException(400, f"theme must be one of {', '.join(THEMES)}")
            patch["theme"] = body.theme
        if body.lang is not None:
            if body.lang not in LANGS:
                raise HTTPException(400, f"lang must be one of {', '.join(LANGS)}")
            patch["lang"] = body.lang
        async with db.conn() as c:     # the development identity may have no row yet
            await c.execute("INSERT INTO users(username, prefs) VALUES ($1, $2) "
                            "ON CONFLICT (username) DO UPDATE SET prefs = users.prefs || excluded.prefs", who, patch)
        return {"saved": True}

    @api.post("/api/farm/me/password")
    async def change_password(body: PasswordIn, x_comfyfarm_user: str | None = Header(default=None)):
        """Ends every session of this account, this one included: the browser logs in again."""
        who = ctx.identity(x_comfyfarm_user)
        async with db.conn() as c:
            u = await c.fetchrow("SELECT password_hash FROM users WHERE username = $1 AND disabled_at IS NULL", who)
            if u is None or not u["password_hash"]:
                raise HTTPException(409, "this account has no local password (signed in through another provider)")
            if not verify_password(body.current, u["password_hash"]):
                raise HTTPException(403, "current password is wrong")
            if problem := password_problem(body.new):
                raise HTTPException(400, f"new password: {problem}")
            await c.execute("UPDATE users SET password_hash = $2, session_epoch = next_session_epoch(session_epoch) WHERE username = $1",
                            who, hash_password(body.new))
        return {"changed": True, "relogin": True}

    return api
