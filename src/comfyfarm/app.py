"""Router process: API + dispatcher + reconcile + health in one asyncio loop (docs/design/01-architecture.md)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .adapter.comfy import ComfyAdapter
from .core import jobs as jobsvc
from .core.dispatcher import Dispatcher
from .core.reconcile import Reconciler
from .core.settings import Settings
from .db import Busy, Database
from .events.hub import EventHub
from .health.monitor import HealthMonitor

log = logging.getLogger(__name__)


class Router:
    def __init__(self, settings: Settings):
        self.s = settings
        self.db = Database(settings.dsn)
        self.adapter = ComfyAdapter()
        self.dispatcher = Dispatcher(self.db, self.adapter, settings)
        self.reconciler = Reconciler(self.db, self.adapter, settings, self.dispatcher.wake)
        self.health = HealthMonitor(self.db, self.adapter, settings, self.dispatcher.wake)
        self.events = EventHub(self.db, settings, self.dispatcher.wake)
        self.dispatcher.ready = self.events.ready
        self.tasks: list[asyncio.Task] = []

    async def start(self, loops: bool = True):
        await self.db.start()
        if loops:
            self.tasks = [asyncio.create_task(x.run()) for x in (self.health, self.dispatcher, self.reconciler, self.events)]
            self.tasks.append(asyncio.create_task(self.listen()))

    async def listen(self):
        """Wake the dispatcher on NOTIFY from any router sharing the ledger. Only an accelerator (docs/design/01):
        notifications sent while this connection is down are lost, so the dispatcher keeps polling."""
        import asyncpg
        while True:
            conn = None
            try:
                conn = await asyncpg.connect(self.s.dsn)
                await conn.add_listener("comfyfarm_wake", lambda *_: self.dispatcher.wake.set())
                while not conn.is_closed():
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("listen connection failed: %s", type(e).__name__)
            finally:
                if conn is not None and not conn.is_closed():
                    await conn.close()
            await asyncio.sleep(2)

    async def stop(self):
        await self.events.stop()
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self.adapter.close()
        await self.db.close()


# Credentials the stock frontend puts into extra_data (Comfy.org API nodes). Kept in memory until dispatch, never
# written to the ledger (D-29). A router restart before dispatch drops them: those API nodes then fail visibly.
SECRET_EXTRA_KEYS = ("auth_token_comfy_org", "api_key_comfy_org")


class GroupIn(BaseModel):
    name: str
    requirements: dict = {}


class WorkerIn(BaseModel):
    name: str
    base_url: str
    group: str | None = None


class WorkerPatch(BaseModel):
    maintenance: bool | None = None
    enabled: bool | None = None
    group: str | None = None


class PromptIn(BaseModel):                 # stock ComfyUI /prompt body + optional ComfyFarm fields
    prompt: dict
    client_id: str | None = None
    prompt_id: str | None = None           # idempotency key chosen by the client (D-23)
    extra_data: dict = {}
    front: bool = False
    comfyfarm_group: int | None = None
    comfyfarm_requirements: dict = {}


def create_app(settings: Settings) -> FastAPI:
    router = Router(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await router.start()
        app.state.router = router
        yield
        await router.stop()

    app = FastAPI(title="ComfyFarm", lifespan=lifespan)
    from .api.stock import make_router
    from .identity import TrustGatewayHeaders, current_roles
    app.add_middleware(TrustGatewayHeaders, secret=settings.gateway_secret, dev=settings.dev_identity is not None)

    def identity(user: str | None) -> str:
        """The user named by the gateway (header kept only if the gateway vouched, see identity.py), or the development
        identity on a loopback-only deployment."""
        if user:
            return user
        if settings.dev_identity is None:
            raise HTTPException(401, "not authenticated")
        return settings.dev_identity

    app.include_router(make_router(router, lambda user: identity(user)))

    @app.exception_handler(Busy)
    async def busy(_req: Request, _exc: Busy):
        return JSONResponse({"error": "busy"}, status_code=503, headers={"Retry-After": "2"})

    async def validate_on_group(group_id: int | None, prompt: dict) -> tuple[str, dict]:
        """D-24: validate on up to 3 online workers of the target group (CPU-only UI workers cannot: some nodes only
        import with a GPU). Idle workers first; 4 s each; nobody answering → skip, the worker validates at dispatch."""
        async with router.db.conn() as c:
            gid = group_id if group_id is not None and await c.fetchval("SELECT 1 FROM worker_groups WHERE id = $1", group_id) \
                else await c.fetchval("SELECT id FROM worker_groups WHERE name = $1", settings.default_group) if settings.default_group \
                else None
            ws = await c.fetch("SELECT base_url FROM workers WHERE enabled AND deleted_at IS NULL AND NOT maintenance "
                               "AND state = 'online' AND ($1::bigint IS NULL OR group_id = $1) "
                               "ORDER BY current_job_id IS NOT NULL, random() LIMIT 3", gid)
        for w in ws:
            verdict, body = await router.adapter.validate(w["base_url"], prompt)
            if verdict != "unavailable":
                return verdict, body
        return "skipped", {}

    @app.post("/api/prompt")
    async def post_prompt(body: PromptIn, x_comfyfarm_user: str | None = Header(default=None)):
        owner = identity(x_comfyfarm_user)
        if body.prompt_id is not None:
            try:
                uuid.UUID(body.prompt_id)
            except ValueError:
                raise HTTPException(400, {"error": {"type": "invalid_prompt_id", "message": "prompt_id must be a UUID"},
                                          "node_errors": {}})
        extra = dict(body.extra_data)
        secrets = {k: extra.pop(k) for k in SECRET_EXTRA_KEYS if k in extra}    # D-29: not persisted
        verdict, err = await validate_on_group(body.comfyfarm_group, body.prompt)
        if verdict == "invalid":                       # stock shape → the frontend highlights the broken nodes
            return JSONResponse({"error": err.get("error"), "node_errors": err.get("node_errors", {})}, status_code=400)
        try:
            job, _created = await jobsvc.submit(router.db, owner=owner, prompt=body.prompt, extra_data=extra,
                                                group_id=body.comfyfarm_group, idempotency_key=body.prompt_id,
                                                browser_client_id=body.client_id, front=body.front,
                                                requirements=body.comfyfarm_requirements)
        except jobsvc.Conflict:
            raise HTTPException(409, "prompt_id conflict")
        except jobsvc.BadRequirements as e:
            raise HTTPException(400, {"error": {"type": "invalid_requirements", "message": str(e)}, "node_errors": {}})
        if secrets and _created:
            router.dispatcher.secrets[job["id"]] = secrets
        router.dispatcher.wake.set()
        return {"prompt_id": str(job["prompt_id"]), "number": job["id"], "node_errors": {}}

    @app.get("/api/farm/jobs/{prompt_id}")
    async def get_job(prompt_id: uuid.UUID, x_comfyfarm_user: str | None = Header(default=None)):
        owner = identity(x_comfyfarm_user)
        async with router.db.conn() as c:
            r = await c.fetchrow("SELECT * FROM jobs WHERE prompt_id = $1 AND owner = $2", prompt_id, owner)
        if r is None:
            raise HTTPException(404, "not found")
        d = dict(r)
        d["prompt_id"] = str(d["prompt_id"])
        return {k: (v.isoformat() if hasattr(v, "isoformat") else str(v) if isinstance(v, uuid.UUID) else v) for k, v in d.items()}

    def admin(user: str | None) -> str:
        who = identity(user)
        if "admin" not in current_roles.get() and not (settings.dev_identity and who in settings.dev_admins):
            raise HTTPException(403, "admin only")
        return who

    @app.post("/api/admin/groups")
    async def create_group(body: GroupIn, x_comfyfarm_user: str | None = Header(default=None)):
        admin(x_comfyfarm_user)
        try:
            req = jobsvc.validate_requirements(body.requirements)
        except jobsvc.BadRequirements as e:
            raise HTTPException(400, str(e))
        async with router.db.conn() as c:
            gid = await c.fetchval("INSERT INTO worker_groups(name, requirements) VALUES ($1, $2) "
                                   "ON CONFLICT (name) DO UPDATE SET requirements = excluded.requirements RETURNING id",
                                   body.name, req)
        return {"id": gid, "name": body.name, "requirements": req}

    @app.post("/api/admin/groups/{name}/object_info/refresh")
    async def refresh_object_info(name: str, x_comfyfarm_user: str | None = Header(default=None)):
        """Manual refresh (D-20): after node changes. Never automatic on a timer; a failure keeps the old snapshot."""
        admin(x_comfyfarm_user)
        async with router.db.conn() as c:
            gid = await c.fetchval("SELECT id FROM worker_groups WHERE name = $1", name)
        if gid is None:
            raise HTTPException(404, "unknown group")
        data = await router.refresh_snapshot(gid)
        if data is None:
            raise HTTPException(503, "no worker answered; the previous snapshot is kept")
        return {"refreshed": True, "bytes": len(data)}

    @app.get("/api/admin/workers")
    async def list_workers(x_comfyfarm_user: str | None = Header(default=None)):
        admin(x_comfyfarm_user)
        async with router.db.conn() as c:
            rows = await c.fetch("SELECT w.*, g.name AS group_name FROM workers w LEFT JOIN worker_groups g ON g.id = w.group_id "
                                 "WHERE w.deleted_at IS NULL ORDER BY w.id")
        out = []
        for r in rows:
            d = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(r).items()}
            d["warnings"] = ["no group: receives no jobs"] if r["group_id"] is None else []   # D-03
            out.append(d)
        return out

    @app.post("/api/admin/workers")
    async def create_worker(body: WorkerIn, x_comfyfarm_user: str | None = Header(default=None)):
        admin(x_comfyfarm_user)
        async with router.db.conn() as c:
            gid = await c.fetchval("SELECT id FROM worker_groups WHERE name = $1", body.group) if body.group else None
            if body.group and gid is None:
                raise HTTPException(400, f"unknown group {body.group!r}")
            wid = await c.fetchval("INSERT INTO workers(name, base_url, group_id) VALUES ($1, $2, $3) RETURNING id",
                                   body.name, body.base_url.rstrip("/"), gid)
        return {"id": wid}

    @app.patch("/api/admin/workers/{worker_id}")
    async def patch_worker(worker_id: int, body: WorkerPatch, x_comfyfarm_user: str | None = Header(default=None)):
        admin(x_comfyfarm_user)
        sets, args = [], [worker_id]
        for k in ("maintenance", "enabled"):
            v = getattr(body, k)
            if v is not None:
                args.append(v); sets.append(f"{k} = ${len(args)}")
        async with router.db.conn() as c:
            if body.group is not None:
                gid = await c.fetchval("SELECT id FROM worker_groups WHERE name = $1", body.group)
                if gid is None:
                    raise HTTPException(400, f"unknown group {body.group!r}")
                args.append(gid); sets.append(f"group_id = ${len(args)}")
            if not sets:
                return {"changed": False}
            done = await c.fetchval(f"UPDATE workers SET {', '.join(sets)} WHERE id = $1 AND deleted_at IS NULL RETURNING id", *args)
        if done is None:
            raise HTTPException(404, "not found")
        router.dispatcher.wake.set()
        return {"changed": True}

    @app.delete("/api/admin/workers/{worker_id}")
    async def delete_worker(worker_id: int, x_comfyfarm_user: str | None = Header(default=None)):
        admin(x_comfyfarm_user)
        async with router.db.conn() as c:     # soft delete: its running jobs become worker_lost (D-06b)
            done = await c.fetchval("UPDATE workers SET deleted_at = now(), enabled = false WHERE id = $1 AND deleted_at IS NULL RETURNING id", worker_id)
        if done is None:
            raise HTTPException(404, "not found")
        return {"deleted": True}

    @app.post("/api/farm/jobs/{prompt_id}/cancel")
    async def cancel_job(prompt_id: uuid.UUID, x_comfyfarm_user: str | None = Header(default=None)):
        owner = identity(x_comfyfarm_user)
        async with router.db.conn() as c:
            jid = await c.fetchval("SELECT id FROM jobs WHERE prompt_id = $1 AND owner = $2", prompt_id, owner)
        if jid is None:
            raise HTTPException(404, "not found")
        return {"status": await jobsvc.request_cancel(router.db, router.adapter, jid)}

    @app.websocket("/ws")
    async def browser_ws(ws: WebSocket):
        """Stock-frontend WebSocket. M2-dev identity: `user` query or X-ComfyFarm-User header (gateway replaces this)."""
        owner = ws.headers.get("x-comfyfarm-user") or ((ws.query_params.get("user") or settings.dev_identity)
                                                       if settings.dev_identity else None)
        if owner is None:
            await ws.close(code=4401)
            return
        client_id = ws.query_params.get("clientId") or uuid.uuid4().hex
        await ws.accept()
        conn = router.events.browsers.bind(client_id, owner, ws)
        if conn is None:                                       # clientId bound to someone else (D-28)
            await ws.close(code=4403)
            return
        try:
            async with router.db.conn() as c:
                n = await c.fetchval("SELECT count(*) FROM jobs WHERE owner = $1 AND deleted_at IS NULL AND status IN "
                                     "('pending','paused','dispatching','running','cancelling')", owner)
            await ws.send_json({"type": "status", "data": {"status": {"exec_info": {"queue_remaining": n}}, "sid": client_id}})
            await router.events.catch_up(conn)
            first = True
            while True:
                msg = await ws.receive_text()
                if first:
                    try:
                        data = json.loads(msg)
                        if data.get("type") == "feature_flags":
                            conn.preview_metadata = bool(data.get("data", {}).get("supports_preview_metadata"))
                            await ws.send_json({"type": "feature_flags", "data": {"supports_preview_metadata": True}})
                    except ValueError:
                        pass
                    first = False
        except WebSocketDisconnect:
            pass
        finally:
            router.events.browsers.unbind(conn)

    @app.post("/api/internal/login")
    async def internal_login(request: Request):
        """Local identity provider check, callable only by the gateway (shared secret header, checked here explicitly
        because no user identity is involved yet)."""
        import hmac as _hmac
        from .identity import verify_password
        if not settings.gateway_secret or not _hmac.compare_digest(request.headers.get("x-comfyfarm-gateway-login", ""),
                                                                    settings.gateway_secret):
            raise HTTPException(403, "gateway only")
        body = await request.json()
        async with router.db.conn() as c:
            u = await c.fetchrow("SELECT username, password_hash, roles FROM users WHERE username = $1 AND disabled_at IS NULL",
                                 body.get("username", ""))
        if u is None or not verify_password(body.get("password", ""), u["password_hash"]):
            raise HTTPException(401, "bad credentials")
        return {"user": u["username"], "roles": list(u["roles"])}

    @app.get("/api/farm/groups")
    async def farm_groups(x_comfyfarm_user: str | None = Header(default=None)):
        """Groups for the frontend extension's picker."""
        identity(x_comfyfarm_user)
        async with router.db.conn() as c:
            rows = await c.fetch("SELECT g.id, g.name, g.requirements, count(w.id) FILTER (WHERE w.state = 'online' AND "
                                 "w.enabled AND w.deleted_at IS NULL AND NOT w.maintenance) AS online FROM worker_groups g "
                                 "LEFT JOIN workers w ON w.group_id = g.id GROUP BY g.id ORDER BY g.name")
        return [{"id": r["id"], "name": r["name"], "online": r["online"], "requirements": r["requirements"]} for r in rows]

    @app.get("/api/farm/locate")
    async def locate(filename: str, subfolder: str = "", type: str = "output",
                     x_comfyfarm_user: str | None = Header(default=None)):
        """Which worker holds a result file of this user (temp files exist only on the executing worker)."""
        owner = identity(x_comfyfarm_user)
        item = [{"filename": filename, "subfolder": subfolder, "type": type}]
        async with router.db.conn() as c:
            url = await c.fetchval(
                "SELECT w.base_url FROM jobs j JOIN workers w ON w.id = j.worker_id WHERE j.owner = $1 AND "
                "EXISTS (SELECT 1 FROM jsonb_each(coalesce(j.outputs, '{}'::jsonb)) o, jsonb_each(o.value) k "
                "        WHERE jsonb_typeof(k.value) = 'array' AND k.value @> $2::jsonb) "
                "ORDER BY j.finished_at DESC NULLS LAST LIMIT 1", owner, item)
        if url is None:
            raise HTTPException(404, "not found")
        return {"base_url": url}

    @app.get("/api/health/dispatch")
    async def health_dispatch():
        """Numbers only, per group (docs/design/06). Unknown values are null, never 0."""
        async with router.db.conn() as c:
            rows = await c.fetch("""
              SELECT g.id, g.name,
                (SELECT count(*) FROM jobs j WHERE j.status = 'pending' AND j.group_id = g.id) AS pending,
                (SELECT count(*) FROM workers w WHERE w.group_id = g.id AND w.state = 'online' AND NOT w.maintenance
                   AND w.enabled AND w.deleted_at IS NULL AND w.current_job_id IS NULL) AS idle_workers,
                (SELECT extract(epoch FROM now() - max(j.dispatched_at)) FROM jobs j WHERE j.group_id = g.id) AS since_dispatch,
                (SELECT extract(epoch FROM now() - min(j.created_at)) FROM jobs j WHERE j.status = 'pending' AND j.group_id = g.id) AS oldest_pending,
                (SELECT extract(epoch FROM now() - min(coalesce(j.started_at, j.dispatched_at))) FROM jobs j
                   WHERE j.group_id = g.id AND j.status IN ('running','dispatching')) AS running_longest,
                (SELECT count(*) FROM jobs j WHERE j.status = 'pending' AND j.group_id = g.id AND NOT EXISTS (
                   SELECT 1 FROM workers w WHERE w.group_id = g.id AND w.enabled AND w.deleted_at IS NULL
                     AND w.state = 'online' AND comfyfarm_satisfies(w.capabilities, g.requirements)
                     AND comfyfarm_satisfies(w.capabilities, j.requirements))) AS unsatisfiable
              FROM worker_groups g ORDER BY g.id""")
        out = {}
        for r in rows:
            stalled = None
            if r["pending"] and r["idle_workers"]:
                cands = [x for x in (r["since_dispatch"], r["oldest_pending"]) if x is not None]
                stalled = float(min(cands)) if cands else None
            out[str(r["id"])] = {"pending": r["pending"], "idle_workers": r["idle_workers"],
                                 "dispatch_idle_seconds": stalled,
                                 "running_longest_seconds": float(r["running_longest"]) if r["running_longest"] is not None else None,
                                 "unsatisfiable_pending": r["unsatisfiable"]}
        return {"groups": out}

    return app
