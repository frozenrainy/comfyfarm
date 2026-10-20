"""Router process: API + dispatcher + reconcile + health in one asyncio loop (docs/design/01-architecture.md)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import pathlib
import uuid

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .adapter.comfy import ComfyAdapter
from .core import access
from .core import jobs as jobsvc
from .core.dispatcher import Dispatcher
from .core.reconcile import Reconciler
from .core.settings import Settings
from .db import Busy, Database
from .events.hub import EventHub
from .health.dispatch import prometheus, snapshot as dispatch_snapshot
from .health.monitor import HealthMonitor

log = logging.getLogger(__name__)
DASHBOARD_DIR = pathlib.Path(__file__).parent / "dashboard"


class Router:
    def __init__(self, settings: Settings):
        self.s = settings
        self.db = Database(settings.dsn)
        self.adapter = ComfyAdapter()
        self.dispatcher = Dispatcher(self.db, self.adapter, settings)
        self.reconciler = Reconciler(self.db, self.adapter, settings, self.dispatcher.wake, self.dispatcher.posting)
        self.health = HealthMonitor(self.db, self.adapter, settings, self.dispatcher.wake)
        self.events = EventHub(self.db, settings, self.dispatcher.wake)
        self.dispatcher.ready = self.events.ready
        from .rollout.engine import Engine
        self.rollout = Engine(self)
        self.tasks: list[asyncio.Task] = []
        self.background: list = []              # coroutine functions the API modules add (api/builds.py: first start)
        self.setup_code: str | None = None      # set while the farm has no administrator (first_admin_code)

    async def start(self, loops: bool = True):
        await self.db.start()
        await self.first_admin_code()
        if loops:
            self.tasks = [asyncio.create_task(x.run()) for x in (self.health, self.dispatcher, self.reconciler, self.events,
                                                                  self.rollout)]
            self.tasks.append(asyncio.create_task(self.listen()))
            self.tasks += [asyncio.create_task(f()) for f in self.background]

    async def first_admin_code(self) -> str | None:
        """A fresh install has no administrator: a one-time setup code lets whoever has it create the first one on the
        gateway's login page (easy deployment: no command line). The code goes to this log only — reading the router's
        log needs the host already — and is new on every start until an administrator exists."""
        async with self.db.conn() as c:
            admins = await c.fetchval("SELECT count(*) FROM users WHERE 'admin' = ANY(roles) AND disabled_at IS NULL")
        if admins:
            self.setup_code = None
            return None
        import secrets
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"            # no 0/O, 1/I: it is typed from a log
        raw = "".join(secrets.choice(alphabet) for _ in range(12))
        self.setup_code = "-".join(raw[i:i + 4] for i in range(0, 12, 4))
        log.warning("no administrator yet — open the gateway in a browser and create one with setup code %s "
                    "(valid until an administrator exists; a new code on every start)", self.setup_code)
        return self.setup_code

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
        await self.rollout.stop()
        await self.db.close()


class NoCacheStatic(StaticFiles):
    """Dashboard files are revalidated on every load (ETag → 304): after an upgrade a browser must not mix cached
    modules of the old version with new ones."""

    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "no-cache"
        return r


# Credentials the stock frontend puts into extra_data (Comfy.org API nodes). Kept in memory until a worker accepted the
# job (Dispatcher.prune_secrets), never written to the ledger (D-29). A router restart before dispatch drops them: those API nodes then fail visibly.
SECRET_EXTRA_KEYS = ("auth_token_comfy_org", "api_key_comfy_org")


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
    from .api import account, admin as admin_api, builds as builds_api, dashboard, rollout as rollout_api, setup as setup_api, stock
    from .api.context import Ctx
    from .identity import TrustGatewayHeaders
    app.add_middleware(TrustGatewayHeaders, secret=settings.gateway_secret, dev=settings.dev_identity is not None)
    ctx = Ctx(router, settings)
    identity, is_admin = ctx.identity, ctx.is_admin
    for module in (stock, dashboard, account, admin_api, rollout_api, setup_api, builds_api):
        app.include_router(module.make_router(ctx))

    @app.exception_handler(Busy)
    async def busy(_req: Request, _exc: Busy):
        return JSONResponse({"error": "busy"}, status_code=503, headers={"Retry-After": "2"})

    async def validate_on_group(group_id: int | None, prompt: dict, allowed: list[int] | None) -> tuple[str, dict]:
        """D-24: validate on up to 3 online workers of the target group (CPU-only UI workers cannot: some nodes only
        import with a GPU). Idle workers first; 4 s each; nobody answering → skip, the worker validates at dispatch.
        Without a target group: the site's default group if the user may use it, else their first group."""
        async with router.db.conn() as c:
            gid = group_id
            if gid is None and settings.default_group:
                gid = await c.fetchval("SELECT id FROM worker_groups WHERE name = $1", settings.default_group)
            if allowed is not None and gid not in allowed:
                gid = allowed[0] if allowed else None
            ws = await c.fetch("SELECT base_url FROM workers WHERE enabled AND deleted_at IS NULL AND NOT maintenance "
                               "AND state = 'online' AND ($1::bigint IS NULL OR group_id = $1) "
                               "ORDER BY current_job_id IS NOT NULL, random() LIMIT 3", gid)
        for w in ws:
            verdict, body = await router.adapter.validate(w["base_url"], prompt)
            if verdict != "unavailable":
                return verdict, body
        return "skipped", {}

    def not_permitted(message: str):
        return JSONResponse({"error": {"type": "group_not_permitted", "message": message, "details": "", "extra_info": {}},
                             "node_errors": {}}, status_code=403)

    @app.post("/api/prompt")
    async def post_prompt(body: PromptIn, x_comfyfarm_user: str | None = Header(default=None)):
        owner = identity(x_comfyfarm_user)
        if body.prompt_id is not None:
            try:
                uuid.UUID(body.prompt_id)
            except ValueError:
                raise HTTPException(400, {"error": {"type": "invalid_prompt_id", "message": "prompt_id must be a UUID"},
                                          "node_errors": {}})
        async with router.db.conn() as c:
            allowed = await ctx.allowed(c, owner)
            group_id = body.comfyfarm_group
            if group_id is not None and not await c.fetchval("SELECT 1 FROM worker_groups WHERE id = $1", group_id):
                group_id = None                                                  # unknown group: no group (D-22)
        if why := access.refusal(allowed, group_id):                         # D-34; checked again at insert
            return not_permitted(why)
        extra = dict(body.extra_data)
        secrets = {k: extra.pop(k) for k in SECRET_EXTRA_KEYS if k in extra}    # D-29: not persisted
        verdict, err = await validate_on_group(group_id, body.prompt, allowed)
        if verdict == "invalid":                       # stock shape → the frontend highlights the broken nodes
            return JSONResponse({"error": err.get("error"), "node_errors": err.get("node_errors", {})}, status_code=400)
        try:
            job, _created = await jobsvc.submit(router.db, owner=owner, prompt=body.prompt, extra_data=extra,
                                                group_id=group_id, idempotency_key=body.prompt_id,
                                                browser_client_id=body.client_id, front=body.front,
                                                requirements=body.comfyfarm_requirements,
                                                permissions=lambda c: ctx.allowed(c, owner))
        except jobsvc.NotPermitted as e:              # revoked while the workflow was being validated
            return not_permitted(str(e))
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

    @app.post("/api/farm/jobs/{prompt_id}/cancel")
    async def cancel_job(prompt_id: uuid.UUID, x_comfyfarm_user: str | None = Header(default=None)):
        who = identity(x_comfyfarm_user)
        async with router.db.conn() as c:       # owner, or an admin (operators cancel stuck jobs from the dashboard)
            r = await c.fetchrow("SELECT id, owner FROM jobs WHERE prompt_id = $1 AND ($2 OR owner = $3)",
                                 prompt_id, is_admin(who), who)
        if r is None:
            raise HTTPException(404, "not found")
        status = await jobsvc.request_cancel(router.db, router.adapter, r["id"])
        await router.events.push_status({r["owner"]})
        return {"status": status}

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

    @app.get("/api/farm/groups")
    async def farm_groups(x_comfyfarm_user: str | None = Header(default=None)):
        """Groups for the frontend extension's picker: only those the user may use (D-34)."""
        who = identity(x_comfyfarm_user)
        async with router.db.conn() as c:
            allowed = await ctx.allowed(c, who)
            rows = await c.fetch("SELECT g.id, g.name, g.requirements, count(w.id) FILTER (WHERE w.state = 'online' AND "
                                 "w.enabled AND w.deleted_at IS NULL AND NOT w.maintenance) AS online FROM worker_groups g "
                                 "LEFT JOIN workers w ON w.group_id = g.id WHERE $1::bigint[] IS NULL OR g.id = ANY($1) "
                                 "GROUP BY g.id ORDER BY g.name", allowed)
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
            return await dispatch_snapshot(c)

    @app.get("/metrics")
    async def metrics():
        """Prometheus exposition of the same numbers. A ledger that cannot be read is reported as
        `comfyfarm_ledger_up 0` with a 200, so the scrape itself still succeeds and the outage is visible."""
        try:
            async with router.db.conn() as c:
                snap = await dispatch_snapshot(c, iso=False)
        except Exception as e:
            log.warning("metrics: ledger unreadable: %s", type(e).__name__)
            snap = None
        return PlainTextResponse(prometheus(snap), media_type="text/plain; version=0.0.4; charset=utf-8")

    # ---- dashboard (static, no build step: the farm may have no package registry, D-32) --------------------------
    @app.get("/comfyfarm/dashboard")
    async def dashboard_redirect():
        return RedirectResponse("/comfyfarm/dashboard/", status_code=307)

    app.mount("/comfyfarm/dashboard/", NoCacheStatic(directory=str(DASHBOARD_DIR), html=True), name="dashboard")

    return app
