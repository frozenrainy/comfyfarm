"""Holding dispatch and personal API tokens (docs/design/19, D-56/D-57)."""
from __future__ import annotations

import datetime as dt
import hmac
import re

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from ..core import hold as holdsvc
from ..identity import api_token_hash, current_token, new_api_token
from ..rollout.engine import audit
from .context import Ctx

SCOPES = ("read", "user", "admin")
MAX_TOKENS = 20                       # live tokens per user
DEFAULT_DAYS, MAX_DAYS = 90, 365
TOKEN_NAME = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")
LAST_USED_EVERY = 60                  # seconds: last_used_at is written at most this often per token


class HoldIn(BaseModel):
    held: bool
    reason: str | None = None
    minutes: int | None = None        # the hold ends by itself after this long (None = until released)


class TokenIn(BaseModel):
    name: str
    scope: str = "user"
    days: int = DEFAULT_DAYS


def token_doc(r) -> dict:
    iso = lambda v: v.isoformat() if v else None
    return {"id": r["id"], "user": r["username"], "name": r["name"], "scope": r["scope"], "hint": r["hint"] + "…",
            "created_at": iso(r["created_at"]), "expires_at": iso(r["expires_at"]), "last_used_at": iso(r["last_used_at"]),
            "revoked_at": iso(r["revoked_at"]),
            "expired": r["revoked_at"] is None and r["expires_at"] <= dt.datetime.now(dt.timezone.utc)}


def make_router(ctx: Ctx) -> APIRouter:
    api = APIRouter()
    router, db = ctx.router, ctx.router.db

    # ---- holding dispatch -----------------------------------------------------------------------------------------
    async def hold_doc(c) -> dict:
        h = await holdsvc.current(c)
        n = await c.fetchrow("SELECT count(*) FILTER (WHERE status IN ('dispatching','running','cancelling')) AS running, "
                             "count(*) FILTER (WHERE status = 'pending') AS pending FROM jobs WHERE deleted_at IS NULL")
        return {"held": h is not None, "since": h and h["since"], "by": h and h["by"], "reason": h and h["reason"],
                "until": h and h["until"], "running": n["running"], "pending": n["pending"]}

    @api.get("/api/farm/hold")
    async def read_hold(x_comfyfarm_user: str | None = Header(default=None)):
        """Every user may see that dispatch is held (the dashboard's banner): their jobs wait because of it."""
        ctx.identity(x_comfyfarm_user)
        async with db.conn() as c:
            return await hold_doc(c)

    @api.put("/api/admin/hold")
    async def write_hold(body: HoldIn, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        if body.minutes is not None and not 1 <= body.minutes <= holdsvc.MAX_MINUTES:
            raise HTTPException(400, f"minutes must be 1–{holdsvc.MAX_MINUTES}")
        async with db.conn() as c:
            async with c.transaction():
                if body.held:
                    await holdsvc.hold(c, who, body.reason, body.minutes)
                else:
                    await holdsvc.release(c, who)
            doc = await hold_doc(c)
        router.dispatcher.wake.set()
        return doc

    # ---- API tokens -------------------------------------------------------------------------------------------------
    def not_by_token():
        # a leaked token must not be able to make more of itself or hide by revoking the others
        if current_token.get() is not None:
            raise HTTPException(403, "API tokens are managed from the dashboard (Settings → API tokens), not with a token")

    @api.get("/api/farm/me/tokens")
    async def my_tokens(x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.identity(x_comfyfarm_user)
        async with db.conn() as c:
            rows = await c.fetch("SELECT * FROM api_tokens WHERE username = $1 AND revoked_at IS NULL "
                                 "ORDER BY created_at DESC", who)
        return {"tokens": [token_doc(r) for r in rows], "admin": ctx.is_admin(who), "max": MAX_TOKENS,
                "scopes": list(SCOPES), "default_days": DEFAULT_DAYS, "max_days": MAX_DAYS}

    @api.post("/api/farm/me/tokens")
    async def create_token(body: TokenIn, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.identity(x_comfyfarm_user)
        not_by_token()
        name = body.name.strip()
        if not TOKEN_NAME.match(name):
            raise HTTPException(400, "name: 1–64 characters, no control characters")
        if body.scope not in SCOPES:
            raise HTTPException(400, f"scope must be one of {', '.join(SCOPES)}")
        if body.scope == "admin" and not ctx.is_admin(who):
            raise HTTPException(403, "only an administrator can make an admin token")
        if not 1 <= body.days <= MAX_DAYS:
            raise HTTPException(400, f"days must be 1–{MAX_DAYS}")
        token = new_api_token()
        async with db.conn() as c:
            async with c.transaction():
                await c.execute("SELECT pg_advisory_xact_lock(hashtext('comfyfarm tokens ' || $1))", who)
                if not await c.fetchval("SELECT 1 FROM users WHERE username = $1 AND disabled_at IS NULL", who):
                    raise HTTPException(409, "this account cannot hold tokens")     # the development identity has no row
                live = await c.fetchval("SELECT count(*) FROM api_tokens WHERE username = $1 AND revoked_at IS NULL "
                                        "AND expires_at > now()", who)
                if live >= MAX_TOKENS:
                    raise HTTPException(409, f"at most {MAX_TOKENS} live tokens — revoke one first")
                r = await c.fetchrow("INSERT INTO api_tokens(username, name, scope, token_hash, hint, expires_at) "
                                     "VALUES ($1, $2, $3, $4, $5, now() + make_interval(days => $6)) RETURNING *",
                                     who, name, body.scope, api_token_hash(token), token[:10], body.days)
                await audit(c, who, "api token created", f"{r['id']}:{name}", scope=body.scope, days=body.days)
        return token_doc(r) | {"token": token}               # the only time the token itself is shown

    async def revoke(c, who: str, row) -> None:
        await c.execute("UPDATE api_tokens SET revoked_at = now(), revoked_by = $2 WHERE id = $1 AND revoked_at IS NULL",
                        row["id"], who)
        await audit(c, who, "api token revoked", f"{row['id']}:{row['name']}", owner=row["username"])

    @api.delete("/api/farm/me/tokens/{token_id}")
    async def revoke_mine(token_id: int, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.identity(x_comfyfarm_user)
        not_by_token()
        async with db.conn() as c:
            async with c.transaction():
                r = await c.fetchrow("SELECT * FROM api_tokens WHERE id = $1 AND username = $2 AND revoked_at IS NULL",
                                     token_id, who)
                if r is None:
                    raise HTTPException(404, "not found")
                await revoke(c, who, r)
        return {"revoked": True}

    @api.get("/api/admin/tokens")
    async def all_tokens(x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            rows = await c.fetch("SELECT * FROM api_tokens WHERE revoked_at IS NULL ORDER BY username, created_at DESC")
        return {"tokens": [token_doc(r) for r in rows]}

    @api.delete("/api/admin/tokens/{token_id}")
    async def revoke_any(token_id: int, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        not_by_token()
        async with db.conn() as c:
            async with c.transaction():
                r = await c.fetchrow("SELECT * FROM api_tokens WHERE id = $1 AND revoked_at IS NULL", token_id)
                if r is None:
                    raise HTTPException(404, "not found")
                await revoke(c, who, r)
        return {"revoked": True}

    @api.post("/api/internal/token")
    async def internal_token(request: Request):
        """The gateway asks who a bearer token belongs to (cached SESSION_TTL there). The roles are the user's
        current ones, cut down to the token's scope: disabling the user or demoting them reaches every token."""
        s = ctx.settings.gateway_secret
        if not s or not hmac.compare_digest(request.headers.get("x-comfyfarm-gateway-login", "").encode("utf-8", "replace"),
                                            s.encode()):
            raise HTTPException(403, "gateway only")
        token = str((await request.json()).get("token") or "")
        async with db.conn() as c:
            r = await c.fetchrow("SELECT t.id, t.name, t.scope, t.username, t.last_used_at, u.roles FROM api_tokens t "
                                 "JOIN users u ON u.username = t.username WHERE t.token_hash = $1 AND t.revoked_at IS NULL "
                                 "AND t.expires_at > now() AND u.disabled_at IS NULL", api_token_hash(token))
            if r is None:
                raise HTTPException(401, "unknown, revoked or expired token")
            await c.execute(f"UPDATE api_tokens SET last_used_at = now() WHERE id = $1 AND (last_used_at IS NULL OR "
                            f"last_used_at < now() - interval '{LAST_USED_EVERY} seconds')", r["id"])
        roles = set(r["roles"])
        if r["scope"] != "admin":
            roles.discard("admin")
        return {"user": r["username"], "roles": sorted(roles), "scope": r["scope"], "token": f"{r['id']}:{r['name']}"}

    return api
