"""System administration (admin only): worker groups, workers, user accounts, statistics."""
from __future__ import annotations

import re

import asyncpg
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from ..core import access, consistency
from ..core import jobs as jobsvc
from ..core import stats as statsvc
from ..identity import hash_password
from .account import password_problem
from .context import Ctx

ADMIN_SET_LOCK = 0x636F6D6661   # advisory lock: changes to who is an active admin ("comfa")
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
# Site settings: key → (type, default, description). Unknown keys are refused.
SETTINGS = {
    "login_user_list": (bool, False, "Login page offers the user names as a drop-down (anyone who can open the login "
                                     "page sees them; passwords are still required)"),
}


async def get_setting(c, key: str):
    v = await c.fetchval("SELECT value FROM settings WHERE key = $1", key)
    return SETTINGS[key][1] if v is None else v


class GroupIn(BaseModel):
    name: str
    requirements: dict = {}


class GroupPatch(BaseModel):
    name: str | None = None
    requirements: dict | None = None


class WorkerIn(BaseModel):
    name: str
    base_url: str
    group: str | None = None
    kind: str = "gpu"                      # gpu | ui (serves the screen and the users' files; never gets a job)

UI_NO_GROUP = "a UI worker belongs to no group: it serves the screen, jobs go to GPU workers"


class WorkerPatch(BaseModel):
    maintenance: bool | None = None
    enabled: bool | None = None
    group: str | None = None               # "" = no group
    name: str | None = None
    base_url: str | None = None
    note: str | None = None                # "" clears


class UserIn(BaseModel):
    username: str
    password: str
    admin: bool = False
    groups: list[int] = []


class UserPatch(BaseModel):
    password: str | None = None
    admin: bool | None = None
    groups: list[int] | None = None
    disabled: bool | None = None


def iso(t):
    return t.isoformat() if t else None


def security_warnings(caps: dict) -> list[str]:
    """docs/design/09: a worker is 'secured' only when the comfyfarm_worker extension enforces a source allow-list.
    Unknown (worker never probed) says nothing rather than crying wolf."""
    ext = caps.get("extension")
    if ext is None:
        return []
    if not ext.get("installed"):
        return ["worker extension not installed: open to everyone on the network, no reconnect fix"]
    out = []
    if not ext.get("secured"):
        out.append("unsecured: accepts requests from any address — set COMFYFARM_ALLOW to the router and gateway"
                   + (f" (invalid entries: {ext['invalid']})" if ext.get("invalid") else ""))
    if not ext.get("ws_fix"):
        out.append("reconnect fix not active on this ComfyUI version: events may stop after a reconnect")
    return out


def check_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+", url):
        raise HTTPException(400, "base_url must look like http://host:port")
    return url


def make_router(ctx: Ctx) -> APIRouter:
    api = APIRouter()
    router, db = ctx.router, ctx.router.db

    # ---- groups -----------------------------------------------------------------------------------------------
    @api.get("/api/admin/groups")
    async def list_groups(x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            rows = await c.fetch(
                "SELECT g.id, g.name, g.requirements, "
                "(SELECT count(*) FROM workers w WHERE w.group_id = g.id AND w.deleted_at IS NULL) AS workers, "
                "(SELECT count(*) FROM jobs j WHERE j.group_id = g.id AND j.status NOT IN ('completed','failed','cancelled')) AS active_jobs, "
                "(SELECT count(*) FROM user_groups u WHERE u.group_id = g.id) AS users, "
                "(SELECT fetched_at FROM object_info_snapshots s WHERE s.group_id = g.id) AS nodes_at "
                "FROM worker_groups g ORDER BY g.name")
        return [{"id": r["id"], "name": r["name"], "requirements": r["requirements"], "workers": r["workers"],
                 "active_jobs": r["active_jobs"], "users": r["users"], "nodes_at": iso(r["nodes_at"])} for r in rows]

    def group_req(req):
        try:
            return jobsvc.validate_requirements(req)
        except jobsvc.BadRequirements as e:
            raise HTTPException(400, str(e))

    @api.post("/api/admin/groups")
    async def create_group(body: GroupIn, x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "name is empty")
        req = group_req(body.requirements)
        async with db.conn() as c:
            try:
                gid = await c.fetchval("INSERT INTO worker_groups(name, requirements) VALUES ($1, $2) RETURNING id", name, req)
            except asyncpg.UniqueViolationError:
                raise HTTPException(409, f"group {name!r} exists")
        return {"id": gid, "name": name, "requirements": req}

    @api.patch("/api/admin/groups/{group_id}")
    async def patch_group(group_id: int, body: GroupPatch, x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            if await c.fetchval("SELECT 1 FROM worker_groups WHERE id = $1", group_id) is None:
                raise HTTPException(404, "not found")
            try:
                if body.name is not None and body.name.strip():
                    await c.execute("UPDATE worker_groups SET name = $2 WHERE id = $1", group_id, body.name.strip())
            except asyncpg.UniqueViolationError:
                raise HTTPException(409, f"group {body.name!r} exists")
            if body.requirements is not None:
                await c.execute("UPDATE worker_groups SET requirements = $2 WHERE id = $1", group_id, group_req(body.requirements))
        router.dispatcher.wake.set()
        return {"changed": True}

    @api.delete("/api/admin/groups/{group_id}")
    async def delete_group(group_id: int, x_comfyfarm_user: str | None = Header(default=None)):
        """Only empty groups: deleting one with jobs would turn them into ungrouped jobs that may run anywhere, and
        its workers would silently stop receiving work."""
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            async with c.transaction():
                g = await c.fetchrow("SELECT id FROM worker_groups WHERE id = $1 FOR UPDATE", group_id)
                if g is None:
                    raise HTTPException(404, "not found")
                workers = await c.fetchval("SELECT count(*) FROM workers WHERE group_id = $1 AND deleted_at IS NULL", group_id)
                jobs = await c.fetchval("SELECT count(*) FROM jobs WHERE group_id = $1 AND status NOT IN "
                                        "('completed','failed','cancelled')", group_id)
                if workers or jobs:
                    raise HTTPException(409, f"group still has {workers} worker(s) and {jobs} unfinished job(s); "
                                             "move or remove them first")
                await c.execute("UPDATE workers SET group_id = NULL WHERE group_id = $1", group_id)   # deleted ones
                await c.execute("DELETE FROM worker_groups WHERE id = $1", group_id)
        return {"deleted": True}

    @api.post("/api/admin/groups/{name}/object_info/refresh")
    async def refresh_object_info(name: str, x_comfyfarm_user: str | None = Header(default=None)):
        """Manual refresh (D-20): after node changes. Never automatic on a timer; a failure keeps the old snapshot."""
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            gid = await c.fetchval("SELECT id FROM worker_groups WHERE name = $1", name)
        if gid is None:
            raise HTTPException(404, "unknown group")
        data = await router.refresh_snapshot(gid)
        if data is None:
            raise HTTPException(503, "no worker answered; the previous snapshot is kept")
        return {"refreshed": True, "bytes": len(data)}

    # ---- workers ----------------------------------------------------------------------------------------------
    @api.get("/api/admin/workers")
    async def list_workers(x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            rows = await c.fetch("SELECT w.*, g.name AS group_name, comfyfarm_satisfies(w.capabilities, g.requirements) AS fits, "
                                 "j.prompt_id AS job_prompt_id, j.owner AS job_owner, j.progress AS job_progress "
                                 "FROM workers w LEFT JOIN worker_groups g ON g.id = w.group_id "
                                 "LEFT JOIN jobs j ON j.id = w.current_job_id WHERE w.deleted_at IS NULL ORDER BY w.name")
        streams = router.events.streams
        rep = consistency.report([dict(r) for r in rows])
        out = []
        for r in rows:
            warn = []
            if r["group_id"] is None and r["kind"] == "gpu":
                warn.append("no group: receives no jobs")                          # D-03
            elif r["fits"] is False and r["capabilities"]:
                warn.append("does not meet its group's requirements: receives no jobs")
            if r["enabled"] and r["state"] == "online" and r["id"] in streams and not streams[r["id"]][0].connected:
                warn.append("event stream down: no progress or previews")
            caps = r["capabilities"] or {}
            warn += security_warnings(caps)
            warn += consistency.warnings(rep.get(r["group_id"], {}).get("workers", {}).get(r["id"]))
            devs = [d for d in caps.get("devices") or [] if isinstance(d, dict)]
            vram = max((d.get("vram_total") or 0 for d in devs), default=0)
            out.append({"id": r["id"], "name": r["name"], "kind": r["kind"], "base_url": r["base_url"], "group": r["group_name"],
                        "group_id": r["group_id"], "state": r["state"], "enabled": r["enabled"],
                        "maintenance": r["maintenance"], "note": r["note"], "warnings": warn,
                        "failing_since": iso(r["failing_since"]), "last_ok_at": iso(r["last_ok_at"]),
                        "backend": caps.get("backend"), "gpu": devs[0].get("name") if devs else None,
                        "vram_gb": round(vram / 2**30, 1) if vram else None, "comfyui": caps.get("comfyui"),
                        "capabilities": caps,
                        "job": {"prompt_id": str(r["job_prompt_id"]), "owner": r["job_owner"], "progress": r["job_progress"]}
                        if r["job_prompt_id"] else None})
        return out

    async def group_id_of(c, name: str | None):
        if not name:
            return None
        gid = await c.fetchval("SELECT id FROM worker_groups WHERE name = $1", name)
        if gid is None:
            raise HTTPException(400, f"unknown group {name!r}")
        return gid

    @api.post("/api/admin/workers")
    async def create_worker(body: WorkerIn, x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        url = check_url(body.base_url)
        if body.kind not in ("gpu", "ui"):
            raise HTTPException(400, "kind: gpu or ui")
        async with db.conn() as c:
            gid = await group_id_of(c, body.group)
            try:
                wid = await c.fetchval("INSERT INTO workers(name, base_url, group_id, kind) VALUES ($1, $2, $3, $4) RETURNING id",
                                       body.name.strip(), url, gid, body.kind)
            except asyncpg.UniqueViolationError:
                raise HTTPException(409, "a worker with this name or URL exists")
            except asyncpg.CheckViolationError:
                raise HTTPException(400, UI_NO_GROUP)
        return {"id": wid}

    @api.patch("/api/admin/workers/{worker_id}")
    async def patch_worker(worker_id: int, body: WorkerPatch, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        sets, args = [], [worker_id]

        def put(col, v):
            args.append(v); sets.append(f"{col} = ${len(args)}")
        for k in ("maintenance", "enabled"):
            if getattr(body, k) is not None:
                put(k, getattr(body, k))
        if body.name is not None and body.name.strip():
            put("name", body.name.strip())
        if body.base_url is not None:
            put("base_url", check_url(body.base_url))
            sets.append("capabilities = '{}'::jsonb")         # a different machine: probe it again
        if body.note is not None:
            put("note", body.note.strip()[:jobsvc.NOTE_MAX] or None)
        async with db.conn() as c:
            if body.group is not None:
                put("group_id", await group_id_of(c, body.group))
            if not sets:
                return {"changed": False}
            async with c.transaction():
                await c.execute("SELECT 1 FROM workers WHERE id = $1 FOR UPDATE", worker_id)   # vs. a step starting now
                # a rollout step owns the worker's maintenance flag and address: turning maintenance off mid-step lets a
                # job be dispatched that the restart kills; a new address makes the step watch another machine
                if (body.maintenance is not None or body.base_url is not None) and await c.fetchval(
                        "SELECT 1 FROM rollout_steps s JOIN rollouts r ON r.id = s.rollout_id WHERE s.worker_id = $1 "
                        "AND r.status IN ('running','aborting','interrupted') AND (s.status IN ('draining','restarting',"
                        "'starting','verifying','reverting') OR (s.status = 'pending' AND s.was_maintenance IS NOT NULL))",
                        worker_id):
                    raise HTTPException(409, "the worker is in a rollout step: wait for it, or abort the rollout")
                try:
                    done = await c.fetchval(f"UPDATE workers SET {', '.join(sets)} WHERE id = $1 AND deleted_at IS NULL "
                                            "RETURNING name", *args)
                except asyncpg.UniqueViolationError:
                    raise HTTPException(409, "a worker with this name or URL exists")
                except asyncpg.CheckViolationError:
                    raise HTTPException(400, UI_NO_GROUP)
                if done is not None:
                    await c.execute("INSERT INTO audit_log(actor, action, target, detail) VALUES ($1, 'worker changed', $2, $3)",
                                    who, done, {k: v for k, v in body.model_dump().items() if v is not None})
        if done is None:
            raise HTTPException(404, "not found")
        router.dispatcher.wake.set()
        return {"changed": True}

    @api.delete("/api/admin/workers/{worker_id}")
    async def delete_worker(worker_id: int, x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:     # soft delete: its running jobs become worker_lost (D-06b); name/URL reusable
            done = await c.fetchval("UPDATE workers SET deleted_at = now(), enabled = false WHERE id = $1 AND deleted_at IS NULL "
                                    "RETURNING id", worker_id)
        if done is None:
            raise HTTPException(404, "not found")
        return {"deleted": True}

    # ---- users ------------------------------------------------------------------------------------------------
    @api.get("/api/admin/users")
    async def list_users(x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            rows = await c.fetch(
                "SELECT u.username, u.roles, u.password_hash IS NOT NULL AS local, u.disabled_at, u.created_at, "
                "coalesce(array_agg(ug.group_id ORDER BY ug.group_id) FILTER (WHERE ug.group_id IS NOT NULL), '{}') AS groups, "
                "(SELECT max(created_at) FROM jobs j WHERE j.owner = u.username) AS last_job_at "
                "FROM users u LEFT JOIN user_groups ug ON ug.username = u.username GROUP BY u.username ORDER BY u.username")
        return [{"username": r["username"], "admin": "admin" in r["roles"], "local": r["local"],
                 "disabled": r["disabled_at"] is not None, "groups": list(r["groups"]), "created_at": iso(r["created_at"]),
                 "last_job_at": iso(r["last_job_at"])} for r in rows]

    async def set_groups(c, username: str, groups: list[int]):
        known = {r["id"] for r in await c.fetch("SELECT id FROM worker_groups WHERE id = ANY($1)", groups)}
        if missing := set(groups) - known:
            raise HTTPException(400, f"unknown group id(s) {sorted(missing)}")
        await c.execute("DELETE FROM user_groups WHERE username = $1", username)
        await c.executemany("INSERT INTO user_groups(username, group_id) VALUES ($1, $2)", [(username, g) for g in sorted(known)])

    async def other_active_admins(c, username: str) -> int:
        """Counted under a transaction lock: two admins demoting each other at once would otherwise both see the
        other one still active and leave the farm with none."""
        await c.execute("SELECT pg_advisory_xact_lock($1)", ADMIN_SET_LOCK)
        return await c.fetchval("SELECT count(*) FROM users WHERE 'admin' = ANY(roles) AND disabled_at IS NULL "
                                "AND username <> $1", username)

    @api.post("/api/admin/users")
    async def create_user(body: UserIn, x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        if not USERNAME.match(body.username):
            raise HTTPException(400, "username: letters, digits and . _ @ - (max 64), starting with a letter or digit")
        if problem := password_problem(body.password):
            raise HTTPException(400, f"password: {problem}")
        async with db.conn() as c:
            async with c.transaction():
                try:
                    await c.execute("INSERT INTO users(username, password_hash, roles) VALUES ($1, $2, $3)",
                                    body.username, hash_password(body.password), ["admin"] if body.admin else [])
                except asyncpg.UniqueViolationError:
                    raise HTTPException(409, f"user {body.username!r} exists")
                await set_groups(c, body.username, body.groups)
        return {"created": True}

    @api.patch("/api/admin/users/{username}")
    async def patch_user(username: str, body: UserPatch, x_comfyfarm_user: str | None = Header(default=None)):
        """Lock-out guards: nobody removes their own admin role or disables themselves, and the last active admin
        cannot be demoted or disabled — a farm without an admin can only be repaired from the database."""
        me = ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            async with c.transaction():
                u = await c.fetchrow("SELECT roles, disabled_at FROM users WHERE username = $1 FOR UPDATE", username)
                if u is None:
                    raise HTTPException(404, "not found")
                is_admin = "admin" in u["roles"]
                losing_admin = (body.admin is False and is_admin) or (body.disabled is True and is_admin)
                if username == me and (body.admin is False or body.disabled is True):
                    raise HTTPException(400, "you cannot remove your own admin role or disable yourself")
                if losing_admin and await other_active_admins(c, username) == 0:
                    raise HTTPException(409, "this is the last active admin")
                bump = False
                if body.password is not None:
                    if problem := password_problem(body.password):
                        raise HTTPException(400, f"password: {problem}")
                    await c.execute("UPDATE users SET password_hash = $2 WHERE username = $1", username, hash_password(body.password))
                    bump = True
                if body.disabled is not None:
                    await c.execute("UPDATE users SET disabled_at = CASE WHEN $2 THEN coalesce(disabled_at, now()) END "
                                    "WHERE username = $1", username, body.disabled)
                    bump = bump or body.disabled
                if body.admin is not None:
                    roles = sorted((set(u["roles"]) | {"admin"}) if body.admin else (set(u["roles"]) - {"admin"}))
                    await c.execute("UPDATE users SET roles = $2 WHERE username = $1", username, roles)
                if body.groups is not None:
                    await set_groups(c, username, body.groups)
                if bump:                                   # every session of the account ends (D-35)
                    await c.execute("UPDATE users SET session_epoch = next_session_epoch(session_epoch) WHERE username = $1", username)
                if body.admin is not None or body.groups is not None:
                    await access.restamp(c, username)      # queued jobs follow the new permissions now (D-34)
        router.dispatcher.wake.set()
        return {"changed": True}

    @api.delete("/api/admin/users/{username}")
    async def delete_user(username: str, x_comfyfarm_user: str | None = Header(default=None)):
        """Removes the account (its jobs and history stay, under the same owner name)."""
        me = ctx.admin(x_comfyfarm_user)
        if username == me:
            raise HTTPException(400, "you cannot delete yourself")
        async with db.conn() as c:
            async with c.transaction():
                u = await c.fetchrow("SELECT roles FROM users WHERE username = $1 FOR UPDATE", username)
                if u is None:
                    raise HTTPException(404, "not found")
                if "admin" in u["roles"] and await other_active_admins(c, username) == 0:
                    raise HTTPException(409, "this is the last active admin")
                await c.execute("DELETE FROM users WHERE username = $1", username)
        return {"deleted": True}

    # ---- site settings ------------------------------------------------------------------------------------------
    @api.get("/api/admin/settings")
    async def read_settings(x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            return {k: {"value": await get_setting(c, k), "default": d, "description": text}
                    for k, (_t, d, text) in SETTINGS.items()}

    @api.put("/api/admin/settings")
    async def write_settings(body: dict, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        for k, v in body.items():
            if k not in SETTINGS:
                raise HTTPException(400, f"unknown setting {k!r}")
            if not isinstance(v, SETTINGS[k][0]):
                raise HTTPException(400, f"{k} must be {SETTINGS[k][0].__name__}")
        async with db.conn() as c:
            async with c.transaction():
                for k, v in body.items():
                    await c.execute("INSERT INTO settings(key, value, updated_by) VALUES ($1, $2, $3) ON CONFLICT (key) "
                                    "DO UPDATE SET value = excluded.value, updated_at = now(), updated_by = excluded.updated_by",
                                    k, v, who)
                    await c.execute("INSERT INTO audit_log(actor, action, target, detail) VALUES ($1, 'setting changed', $2, $3)",
                                    who, k, {"value": v})
        return {"saved": True}

    # ---- statistics -------------------------------------------------------------------------------------------
    @api.get("/api/admin/stats")
    async def stats(x_comfyfarm_user: str | None = Header(default=None), days: int | None = None,
                    start: str | None = None, end: str | None = None, tz: str | None = None):
        ctx.admin(x_comfyfarm_user)
        try:
            since, until, zone = statsvc.period(days, start, end, tz)
        except statsvc.BadRange as e:
            raise HTTPException(400, str(e))
        async with db.conn() as c:
            return await statsvc.compute(c, since, until, zone)

    return api
