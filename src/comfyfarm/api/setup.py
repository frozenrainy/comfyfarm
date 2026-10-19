"""Worker setup (docs/design/14): the dashboard's *Set up a worker*. An admin describes the host; the router makes a
setup script for it (one-time token, checksum) that the operator runs with sudo — or that the router runs over ssh
with an account and password the operator types in and the router forgets (D-41). The script reports back with the
host check and the host's ssh keys; the dashboard then adds the worker through the usual endpoints.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import re
import secrets
from importlib import metadata

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .. import launcher
from ..core import hostsetup
from ..rollout import backends as be
from ..rollout import render as rd
from ..rollout.engine import audit
from .context import Ctx

TOKEN_TTL = dt.timedelta(hours=2)
ACCOUNT = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
GPU = ("amd", "nvidia", "none", "any")
REPORT_LIMIT = 256 * 1024


class MountIn(BaseModel):
    source: str
    target: str
    options: str | None = None


class SetupIn(BaseModel):
    role: str = "gpu"                           # gpu | ui (the UI worker: the screen and the users' files, no jobs)
    name: str                                   # the worker's name in the dashboard
    host: str                                   # address the router and the gateway reach it at
    ssh_port: int = 22
    account: str = "comfyfarm"
    uid: int | None = 47800                     # the same on every host: shared storage checks numbers, not names
    instance: str
    port: int = 8188
    listen: str = "0.0.0.0"
    gpu: str = "any"
    group: str | None = None
    profile_id: int | None = None
    router_url: str                             # the router as the worker sees it
    paths: dict[str, str]                       # art, local, models, input, output, user, temp
    mounts: list[MountIn] = []
    builds: bool = False                        # allow builds from the dashboard on this host (build.py)
    worker_id: int | None = None                # set up an existing worker again (new tools, other options)


class SshRunIn(BaseModel):
    user: str
    password: str
    port: int | None = None


def version() -> str:
    try:
        return metadata.version("comfyfarm")
    except metadata.PackageNotFoundError:
        return "dev"


def check_spec(body: SetupIn) -> None:
    if not launcher.INSTANCE.match(body.instance):
        raise HTTPException(400, "instance: letters, digits and . _ - (max 63)")
    if not (be.HOST_NAME.match(body.host) or be._is_ip(body.host)):
        raise HTTPException(400, "host: a host name or an IP address")
    if not ACCOUNT.match(body.account) or body.account == "root":
        raise HTTPException(400, "account: a lower-case account name, not root")
    if body.uid is not None and not 1000 <= body.uid <= 60000:
        raise HTTPException(400, "uid: 1000…60000 (the same number on every host that shares storage)")
    if body.role not in ("gpu", "ui"):
        raise HTTPException(400, "role: gpu or ui")
    if body.gpu not in GPU:
        raise HTTPException(400, f"gpu: one of {', '.join(GPU)}")
    if body.role == "ui" and body.group:
        raise HTTPException(400, "a UI worker belongs to no group: it serves the screen, jobs go to GPU workers")
    for p in (body.port, body.ssh_port):
        if not 1 <= p <= 65535:
            raise HTTPException(400, "port out of range")
    if not re.match(r"^https?://[^\s/]+/?$", body.router_url):
        raise HTTPException(400, "router_url: http(s)://host:port as the worker reaches the router")
    if missing := [k for k in rd.PATH_KEYS if not body.paths.get(k)]:
        raise HTTPException(400, f"paths missing: {missing}")
    for k, v in body.paths.items():
        if k not in rd.PATH_KEYS or not v.startswith("/") or "\n" in v:
            raise HTTPException(400, f"paths.{k} must be an absolute path")
    if bad := rd.path_problems(body.paths, False):
        raise HTTPException(400, "; ".join(bad) + " — directories users write to must not overlap the store or the code "
                                 "under the local root (contract §4)")
    for m in body.mounts:
        if not re.fullmatch(r"[A-Za-z0-9._-]+:/\S*", m.source) or not m.target.startswith("/") or " " in m.target:
            raise HTTPException(400, f"mount {m.source} → {m.target}: server:/export → /absolute/mountpoint")
        if m.options and not re.fullmatch(r"[A-Za-z0-9_=,.-]+", m.options):
            raise HTTPException(400, f"mount options {m.options!r}")


def instance_config(spec: dict, extra: dict) -> dict:
    """What PUT /api/admin/instances/{id} gets once the host is set up: the ssh backend with the router's agent key and
    the host keys the setup script reported (pinned in known_hosts)."""
    cfg = {"host": extra["host"], "user": spec["account"], "key": hostsetup.AGENT_KEY, "known_hosts": hostsetup.KNOWN_HOSTS}
    if extra.get("ssh_port", 22) != 22:
        cfg["port"] = extra["ssh_port"]
    return {"instance": spec["instance"], "role": spec.get("role", "gpu"), "platform": "posix", "hosts": [extra["host"]],
            "listen": spec["listen"], "port": spec["port"], "paths": dict(spec["paths"], art=spec["art"], local=spec["local"]),
            "restart_backend": "ssh", "restart_config": cfg}


def make_router(ctx: Ctx) -> APIRouter:
    api = APIRouter()
    db, settings = ctx.router.db, ctx.settings
    running: dict[int, asyncio.Task] = {}

    def public(r) -> dict:
        d = {k: r[k] for k in ("id", "spec", "extra", "script_sha256", "mode", "status", "log", "result", "worker_id",
                               "created_by")}
        for k in ("created_at", "expires_at", "reported_at", "finished_at"):
            d[k] = r[k].isoformat() if r[k] else None
        d["expired"] = r["status"] in ("waiting", "running") and r["expires_at"] < dt.datetime.now(dt.timezone.utc)
        if r["status"] in ("reported", "finished"):
            d["instance_config"] = instance_config(r["spec"], r["extra"])
            d["worker"] = {"name": r["extra"]["name"], "base_url": f"http://{r['extra']['host']}:{r['spec']['port']}",
                           "group": r["extra"].get("group"), "kind": r["spec"].get("role", "gpu")}
        return d

    @api.post("/api/admin/setups")
    async def create(body: SetupIn, request: Request, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        check_spec(body)
        async with db.conn() as c:
            if body.worker_id is not None:              # again: the same worker and instance, nothing else
                same = await c.fetchrow("SELECT w.name, i.instance FROM workers w JOIN instances i ON i.worker_id = w.id "
                                        "WHERE w.id = $1 AND w.deleted_at IS NULL", body.worker_id)
                if same is None or same["instance"] != body.instance or same["name"] != body.name.strip():
                    raise HTTPException(409, "setting a worker up again keeps its name and instance")
            if await c.fetchval("SELECT 1 FROM instances WHERE instance = $1 AND worker_id IS DISTINCT FROM $2",
                                body.instance, body.worker_id):
                raise HTTPException(409, f"instance name {body.instance!r} is taken")
            if await c.fetchval("SELECT 1 FROM workers WHERE deleted_at IS NULL AND (name = $1 OR base_url = $2) "
                                "AND id IS DISTINCT FROM $3", body.name.strip(), f"http://{body.host}:{body.port}",
                                body.worker_id):
                raise HTTPException(409, "a worker with this name or address exists")
        try:
            pub = await hostsetup.agent_public_key(settings.keys_dir)
        except hostsetup.SetupError as e:
            raise HTTPException(400, str(e))
        token = secrets.token_urlsafe(24)
        base = body.router_url.rstrip("/")
        spec = {"instance": body.instance, "role": body.role, "account": body.account, "uid": body.uid, "router_url": base,
                "art": body.paths["art"], "local": body.paths["local"], "gpu": "none" if body.role == "ui" else body.gpu, "port": body.port, "listen": body.listen,
                "paths": {k: v for k, v in body.paths.items() if k not in ("art", "local")},
                "mounts": [m.model_dump(exclude_none=True) for m in body.mounts], "agent_pubkey": pub,
                "builds": body.builds}
        extra = {"name": body.name.strip(), "host": body.host, "ssh_port": body.ssh_port, "group": body.group,
                 "profile_id": body.profile_id, "again": body.worker_id}
        try:
            script, sha = hostsetup.build_script(spec, f"{base}/api/worker_setup/{token}/report", version())
        except hostsetup.SetupError as e:
            raise HTTPException(400, str(e))
        async with db.conn() as c:
            async with c.transaction():
                sid = await c.fetchval(
                    "INSERT INTO worker_setups(token_sha256, spec, extra, script, script_sha256, created_by, expires_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6, now() + $7::interval) RETURNING id",
                    hashlib.sha256(token.encode()).hexdigest(), spec, extra, script, sha, who, TOKEN_TTL)
                await audit(c, who, "worker setup created", body.instance, host=body.host, setup=sid)
        url = f"{base}/api/worker_setup/{token}/script"
        return {"id": sid, "script_url": url, "script_sha256": sha, "command": hostsetup.operator_command(url, sha),
                "expires_in": int(TOKEN_TTL.total_seconds())}

    @api.get("/api/admin/setups")
    async def list_setups(x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            rows = await c.fetch("SELECT * FROM worker_setups ORDER BY id DESC LIMIT 20")
        return [public(r) for r in rows]

    @api.get("/api/admin/setups/{sid}")
    async def get_setup(sid: int, x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            r = await c.fetchrow("SELECT * FROM worker_setups WHERE id = $1", sid)
        if r is None:
            raise HTTPException(404, "no such setup")
        return public(r)

    @api.post("/api/admin/setups/{sid}/ssh")
    async def run_ssh(sid: int, body: SshRunIn, x_comfyfarm_user: str | None = Header(default=None)):
        """D-41, the second way: the router logs in with this account and runs the script with sudo. The password is
        used for this run only — not stored, not logged, not audited."""
        who = ctx.admin(x_comfyfarm_user)
        if not be.SSH_USER.match(body.user):
            raise HTTPException(400, "user: a plain account name")
        async with db.conn() as c:
            async with c.transaction():
                r = await c.fetchrow("SELECT * FROM worker_setups WHERE id = $1 FOR UPDATE", sid)
                if r is None:
                    raise HTTPException(404, "no such setup")
                if body.user == r["spec"]["account"]:
                    raise HTTPException(400, "log in with your own admin account, not the service account "
                                             f"{body.user!r} (setup creates that one for ComfyUI)")
                if r["status"] not in ("waiting", "failed") or r["expires_at"] < dt.datetime.now(dt.timezone.utc):
                    raise HTTPException(409, f"this setup is {r['status']}" + (" (expired)" if r["status"] == "waiting" else ""))
                await c.execute("UPDATE worker_setups SET mode = 'ssh', status = 'running', log = '' WHERE id = $1", sid)
                await audit(c, who, "worker setup over ssh", r["spec"]["instance"], host=r["extra"]["host"], user=body.user)
        port = body.port or r["extra"].get("ssh_port", 22)

        async def go():
            buf: list[str] = []

            async def flush():
                async with db.conn() as c:
                    await c.execute("UPDATE worker_setups SET log = log || $2 WHERE id = $1", sid, "".join(buf))
                buf.clear()

            def log(text: str):
                buf.append(text.replace(body.password, "********") if body.password else text)
            try:
                rc = await hostsetup.run_over_ssh(r["extra"]["host"], port, body.user, body.password, r["script"], log)
            except Exception as e:                                       # noqa: BLE001 — the dashboard shows it
                log(f"{type(e).__name__}: {e}\n")
                rc = 1
            await flush()
            if rc != 0:                                                  # a run that reported has its own status
                async with db.conn() as c:
                    await c.execute("UPDATE worker_setups SET status = 'failed' WHERE id = $1 AND status = 'running'", sid)
        t = asyncio.create_task(go())
        running[sid] = t
        t.add_done_callback(lambda _t: running.pop(sid, None))
        return {"started": True}

    @api.post("/api/admin/setups/{sid}/renew")
    async def renew(sid: int, x_comfyfarm_user: str | None = Header(default=None)):
        """A new command for an unfinished setup (the dialog was closed; only the token's hash is kept, so the old command
        cannot be shown again). Same parameters, a new token and checksum; the old token stops working."""
        who = ctx.admin(x_comfyfarm_user)
        token = secrets.token_urlsafe(24)
        async with db.conn() as c:
            async with c.transaction():
                r = await c.fetchrow("SELECT * FROM worker_setups WHERE id = $1 FOR UPDATE", sid)
                if r is None:
                    raise HTTPException(404, "no such setup")
                if r["status"] not in ("waiting", "failed"):
                    raise HTTPException(409, f"this setup is {r['status']}")
                base = r["spec"]["router_url"]
                try:
                    script, sha = hostsetup.build_script(r["spec"], f"{base}/api/worker_setup/{token}/report", version())
                except hostsetup.SetupError as e:
                    raise HTTPException(400, str(e))
                await c.execute("UPDATE worker_setups SET token_sha256 = $2, script = $3, script_sha256 = $4, status = 'waiting', "
                                "expires_at = now() + $5::interval WHERE id = $1", sid, hashlib.sha256(token.encode()).hexdigest(),
                                script, sha, TOKEN_TTL)
                await audit(c, who, "worker setup renewed", r["spec"]["instance"], setup=sid)
        url = f"{base}/api/worker_setup/{token}/script"
        return {"id": sid, "script_url": url, "script_sha256": sha, "command": hostsetup.operator_command(url, sha),
                "expires_in": int(TOKEN_TTL.total_seconds())}

    @api.post("/api/admin/setups/{sid}/finished")
    async def mark_finished(sid: int, worker_id: int, x_comfyfarm_user: str | None = Header(default=None)):
        """The dashboard added the worker (POST workers, PUT instance, assign, start): the token stops working."""
        who = ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            async with c.transaction():
                r = await c.fetchrow("UPDATE worker_setups SET status = 'finished', worker_id = $2, finished_at = now() "
                                     "WHERE id = $1 AND status = 'reported' RETURNING spec", sid, worker_id)
                if r is None:
                    raise HTTPException(409, "only a reported setup can be finished")
                await audit(c, who, "worker setup finished", r["spec"]["instance"], worker=worker_id)
        return {"finished": True}

    # ---- the worker host's calls (no user: the token is the credential) --------------------------------------------
    async def by_token(c, token: str, lock: bool = False):
        r = await c.fetchrow("SELECT * FROM worker_setups WHERE token_sha256 = $1" + (" FOR UPDATE" if lock else ""),
                             hashlib.sha256(token.encode()).hexdigest())
        if r is None or r["status"] == "finished" or r["expires_at"] < dt.datetime.now(dt.timezone.utc):
            raise HTTPException(404, "unknown or expired setup — make a new one in the dashboard")
        return r

    @api.get("/api/worker_setup/{token}/script")
    async def script(token: str):
        async with db.conn() as c:
            r = await by_token(c, token)
        return PlainTextResponse(r["script"], media_type="text/x-python")

    @api.post("/api/worker_setup/{token}/report")
    async def report(token: str, request: Request):
        raw = await request.body()
        if len(raw) > REPORT_LIMIT:
            raise HTTPException(413, "report too large")
        try:
            body = json.loads(raw)
            assert isinstance(body, dict)
        except (ValueError, AssertionError):
            raise HTTPException(400, "not a JSON object")
        async with db.conn() as c:
            async with c.transaction():
                r = await by_token(c, token, lock=True)
                pinned, note = 0, ""
                try:
                    pinned = hostsetup.pin_host_keys(settings.keys_dir, r["extra"]["host"], body.get("host_keys") or [],
                                                     r["extra"].get("ssh_port", 22))
                except (hostsetup.SetupError, OSError) as e:
                    note = f"host keys not pinned: {e}"
                ok = bool(body.get("ok")) and pinned > 0
                result = {"ok": ok, "check": body.get("check") or [], "steps": body.get("steps") or [],
                          "error": body.get("error") or note or None, "hostname": body.get("hostname"),
                          "version": body.get("version"), "pinned_host_keys": pinned}
                await c.execute("UPDATE worker_setups SET status = $2, result = $3, reported_at = now() WHERE id = $1",
                                r["id"], "reported" if ok else "failed", result)
                await audit(c, "worker setup", "worker setup reported", r["spec"]["instance"], ok=ok, host=r["extra"]["host"])
        return {"received": True}

    return api
