"""Builds on the control plane, node packs picked in the dashboard, and the first start of the UI worker container
(docs/design/15). The router never builds and never writes the artifact store: it writes a request for the `builder`
service and reads what that service writes back (builder.py) — the recipes and the allowed node pack sources are the
builder's, not the router's."""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import secrets
import socket
import time

import asyncpg
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .. import publish
from ..rollout import backends as be
from ..rollout import render as rd
from ..rollout.engine import audit, profile_doc
from .context import Ctx
from .rollout import InstanceIn, check_instance, check_profile

log = logging.getLogger(__name__)
COMMIT = re.compile(r"^[0-9a-f]{40}$")
HTTPS = re.compile(r"^https://[A-Za-z0-9][A-Za-z0-9._~:/@-]*$")
REQUEST = re.compile(r"^[0-9]{10,20}-[0-9a-f]{8}$")
ALIVE = 120.0                       # the builder's heartbeat is every 30 s
LOG_TAIL = 6000
FIRST_START = "comfyfarm (first start)"


class BuildIn(BaseModel):
    recipe: str
    roles: list[str] = ["gpu", "ui"]
    packs: bool = True                  # with the node packs of the dashboard


class ResolveIn(BaseModel):
    repo: str
    ref: str = ""


class PackIn(BaseModel):
    name: str
    repo: str
    commit: str
    ref: str | None = None              # what was typed (a branch or tag): shown, never built from
    ui: bool = True
    requirements_file: str | None = None
    constraints: list[str] = []
    note: str | None = None


class PackPatch(BaseModel):
    commit: str | None = None
    ref: str | None = None
    ui: bool | None = None
    requirements_file: str | None = None
    constraints: list[str] | None = None
    note: str | None = None


def check_pack(p: dict) -> dict:
    if not publish.NAME.match(p["name"]) or p["name"] in ("comfyfarm_worker", "comfyfarm_ui"):
        raise HTTPException(400, "name: letters, digits and . _ - (the pack's directory under custom_nodes)")
    if not HTTPS.match(p["repo"]) or ".." in p["repo"]:
        raise HTTPException(400, "repo: an https:// git URL")
    if not COMMIT.match(p["commit"]):
        raise HTTPException(400, "commit: the full 40-character commit (a pack is pinned)")
    if p.get("requirements_file") and not all(publish.REQ_FILE.match(f.strip()) for f in p["requirements_file"].split(",")):
        raise HTTPException(400, "requirements_file: .txt files inside the pack, comma-separated")
    for x in p.get("constraints") or []:
        if "\n" in x or not publish.SPEC.match(x.strip()):
            raise HTTPException(400, f"constraints: version specifiers like 'kornia<0.8', not {x!r}")
    return p


def templates(art: str | None) -> list[dict]:
    """Profile templates in the store (publish.py recipe), each with the artifacts it names that are not there."""
    out = []
    for f in sorted(glob.glob(os.path.join(art, "profiles", "*.json"))) if art else []:
        try:
            with open(f) as fh:
                t = json.load(fh)
        except (OSError, ValueError):
            continue
        roles = [k for k in ("gpu", "ui") if isinstance(t, dict) and isinstance(t.get(k), dict)]
        if not roles or not isinstance(t.get("template"), str):
            continue
        missing = sorted({f"{kind}/{n}" for role in roles
                          for kind, names in (("core", [t[role].get("core")]), ("venv", [t[role].get("venv")]),
                                              ("nodes", t[role].get("nodes") or []))
                          for n in names if isinstance(n, str) and not os.path.exists(os.path.join(art, kind, n, "READY"))})
        out.append({**t, "missing": missing, "roles": roles})
    return out


def write_builder_request(builds_dir: str, doc: dict) -> str:
    """A request for the builder (docs/design/15): a new file in requests/, written whole then renamed."""
    rid = f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    req = os.path.join(builds_dir, "requests")
    tmp = os.path.join(req, f".{rid}.tmp")
    with open(tmp, "w") as f:
        json.dump(doc, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, os.path.join(req, rid + ".json"))
    return rid


def builder_status(builds_dir: str, rid: str) -> dict | None:
    """What the builder wrote about a request; {"state": "queued"} while it waits; None when it is unknown."""
    try:
        with open(os.path.join(builds_dir, "status", rid + ".json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"id": rid, "state": "queued"} if os.path.exists(os.path.join(builds_dir, "requests", rid + ".json")) else None


def read_policy(builds_dir: str | None) -> dict | None:
    """What the builder says it does (its policy file, rewritten every 30 s), and whether it is up."""
    if not builds_dir:
        return None
    try:
        path = os.path.join(builds_dir, "status", "policy.json")
        with open(path) as f:
            p = json.load(f)
        p["alive_for"] = time.time() - os.path.getmtime(path)
        p["up"] = p["alive_for"] < ALIVE
        return p
    except (OSError, ValueError, TypeError):
        return None


def signing_key(builds_dir: str | None) -> str | None:
    """The builder's public key (its policy): what GPU hosts pin to check their copies of the artifacts with."""
    try:
        with open(os.path.join(builds_dir, "status", "policy.json")) as f:
            key = json.load(f).get("signing_key")
    except (OSError, ValueError, TypeError):
        return None
    return key if isinstance(key, str) and re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+", key) else None


def make_router(ctx: Ctx) -> APIRouter:
    api = APIRouter()
    router, db, s = ctx.router, ctx.router.db, ctx.settings
    first = {"state": "off", "detail": ""}

    # ---- the builder ---------------------------------------------------------------------------------------------
    def dirs() -> tuple[str, str]:
        if not s.builds_dir:
            raise HTTPException(400, "no control-plane builder here (the compose deployment's builder service)")
        return os.path.join(s.builds_dir, "requests"), os.path.join(s.builds_dir, "status")

    def policy() -> dict | None:
        return read_policy(s.builds_dir)

    def need_builder() -> dict:
        dirs()
        p = policy()
        if not p or not p["up"]:
            raise HTTPException(503, "the builder is not running (docker compose ps builder; its log says why)")
        return p

    def write_request(doc: dict) -> str:
        dirs()
        return write_builder_request(s.builds_dir, doc)

    def read_status(rid: str) -> dict:
        if not REQUEST.match(rid):
            raise HTTPException(404, "no such request")
        req, st = dirs()
        try:
            with open(os.path.join(st, rid + ".json")) as f:
                out = json.load(f)
        except (OSError, ValueError):
            if os.path.exists(os.path.join(req, rid + ".json")):
                return {"id": rid, "state": "queued"}
            raise HTTPException(404, "no such request")
        try:
            with open(os.path.join(st, rid + ".log"), "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - LOG_TAIL))
                out["log"] = f.read().decode(errors="replace")
        except OSError:
            pass
        return out

    @api.get("/api/admin/builder")
    async def builder_state(x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        p = policy()
        return {"configured": bool(s.builds_dir), "up": bool(p and p["up"]), "recipes": (p or {}).get("recipes", []),
                "about": (p or {}).get("about", {}),
                "node_sources": (p or {}).get("node_sources", []), "artifacts": bool(s.artifacts_dir),
                "signing": bool((p or {}).get("signing_key")), "first_start": dict(first)}

    @api.get("/api/admin/getting-started")
    async def getting_started(x_comfyfarm_user: str | None = Header(default=None)):
        """Where a new farm stands on its first day, step by step (the dashboard's list): what is done, what is next."""
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            ui = await c.fetchval("SELECT EXISTS (SELECT 1 FROM workers w JOIN instances i ON i.worker_id = w.id WHERE "
                                  "w.kind = 'ui' AND w.deleted_at IS NULL AND i.profile_id IS NOT NULL AND w.state = 'online')")
            gpu_profile = await c.fetchval("SELECT EXISTS (SELECT 1 FROM profiles WHERE NOT (flags @> '[\"--cpu\"]'::jsonb))")
            gpu_worker = await c.fetchval("SELECT EXISTS (SELECT 1 FROM workers w JOIN instances i ON i.worker_id = w.id WHERE "
                                          "w.kind = 'gpu' AND w.deleted_at IS NULL AND w.group_id IS NOT NULL AND "
                                          "i.profile_id IS NOT NULL AND w.state = 'online')")
            user = await c.fetchval("SELECT EXISTS (SELECT 1 FROM users u JOIN user_groups g ON g.username = u.username "
                                    "WHERE NOT ('admin' = ANY(u.roles)) AND u.disabled_at IS NULL)")
            job = await c.fetchval("SELECT EXISTS (SELECT 1 FROM jobs WHERE status = 'completed')")
        return {"first_start": dict(first), "steps": [
            {"key": "ui", "done": ui}, {"key": "build", "done": gpu_profile},
            {"key": "gpu", "done": gpu_worker}, {"key": "user", "done": user}, {"key": "job", "done": job}]}

    @api.get("/api/admin/defaults")
    async def deployment_defaults(x_comfyfarm_user: str | None = Header(default=None)):
        """What the deployment knows that a setup form should not ask a newcomer to guess: the artifact store, the model
        store and the shared storage (the compose paths) and the router's published port."""
        ctx.admin(x_comfyfarm_user)
        return {"art": s.artifacts_dir, "models": s.models_dir, "shared": s.ui_shared, "router": s.public_router}

    @api.post("/api/admin/builds")
    async def request_build(body: BuildIn, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        p = need_builder()
        if body.recipe not in p.get("recipes", []):
            raise HTTPException(400, f"no recipe {body.recipe!r} in the builder (it has: {', '.join(p.get('recipes', []))})")
        if not body.roles or set(body.roles) - set(publish.ROLES):
            raise HTTPException(400, "roles: gpu, ui or both")
        nodes = []
        if body.packs:
            async with db.conn() as c:
                for r in await c.fetch("SELECT * FROM node_packs ORDER BY name"):
                    n = {"name": r["name"], "repo": r["repo"], "commit": r["commit"], "ui": r["ui"]}
                    if r["requirements_file"]:
                        n["requirements_file"] = r["requirements_file"]
                    if r["constraints"]:
                        n["constraints"] = list(r["constraints"])
                    nodes.append(n)
            if nodes and not p.get("node_sources"):
                raise HTTPException(400, "the builder takes no node packs from the dashboard (its COMFYFARM_BUILD_NODE_SOURCES "
                                         "is empty) — build without them, or ask whoever runs it")
        rid = write_request({"kind": "build", "recipe": body.recipe, "roles": body.roles, "nodes": nodes, "by": who})
        async with db.conn() as c:
            await audit(c, who, "build requested", body.recipe, request=rid, roles=body.roles, packs=len(nodes))
        return {"id": rid, "packs": len(nodes)}

    @api.get("/api/admin/builds/{rid}")
    async def build_status(rid: str, x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        return read_status(rid)

    @api.get("/api/admin/builds")
    async def list_builds(x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        if not s.builds_dir:
            return []
        _, st = dirs()
        out = []
        for f in sorted(glob.glob(os.path.join(st, "*.json")), reverse=True):
            rid = os.path.basename(f)[:-5]
            if REQUEST.match(rid):
                try:
                    with open(f) as fh:
                        d = json.load(fh)
                except (OSError, ValueError):
                    continue
                if d.get("kind") == "build":
                    out.append(d)
            if len(out) >= 20:
                break
        return out

    @api.get("/api/admin/templates")
    async def list_templates(x_comfyfarm_user: str | None = Header(default=None)):
        """The store's profile templates as the router sees them (it mounts the store read-only next to the builder)."""
        ctx.admin(x_comfyfarm_user)
        if not s.artifacts_dir:
            raise HTTPException(400, "the router does not see the artifact store (COMFYFARM_ARTIFACTS_DIR)")
        return templates(s.artifacts_dir)

    # ---- node packs ----------------------------------------------------------------------------------------------
    @api.get("/api/admin/node-packs")
    async def list_packs(x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            rows = await c.fetch("SELECT * FROM node_packs ORDER BY name")
        return [{k: (list(r[k]) if k == "constraints" and r[k] is not None else r[k]) for k in
                 ("id", "name", "repo", "commit", "ref", "ui", "requirements_file", "constraints", "note", "added_by")}
                | {"updated_at": r["updated_at"].isoformat()} for r in rows]

    @api.post("/api/admin/node-packs/resolve")
    async def resolve(body: ResolveIn, x_comfyfarm_user: str | None = Header(default=None)):
        """A branch or tag (empty: the default branch) → the commit it names now, asked of the builder (it has the
        network and the allowed sources). Poll GET /api/admin/builds/{id}."""
        who = ctx.admin(x_comfyfarm_user)
        p = need_builder()
        if not p.get("node_sources"):
            raise HTTPException(400, "the builder takes no node packs from the dashboard (COMFYFARM_BUILD_NODE_SOURCES is empty)")
        if not HTTPS.match(body.repo):
            raise HTTPException(400, "repo: an https:// git URL")
        if not any(body.repo.startswith(x) for x in p["node_sources"]):
            raise HTTPException(400, f"{body.repo} is not under the sources the builder allows: {', '.join(p['node_sources'])}")
        return {"id": write_request({"kind": "resolve", "repo": body.repo, "ref": body.ref.strip(), "by": who})}

    @api.post("/api/admin/node-packs")
    async def add_pack(body: PackIn, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        p = check_pack(body.model_dump())
        async with db.conn() as c:
            async with c.transaction():
                try:
                    pid = await c.fetchval(
                        "INSERT INTO node_packs(name, repo, commit, ref, ui, requirements_file, constraints, note, added_by) "
                        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id", p["name"], p["repo"], p["commit"], p["ref"],
                        p["ui"], p["requirements_file"], p["constraints"], p["note"], who)
                except asyncpg.UniqueViolationError:
                    raise HTTPException(409, f"a node pack named {p['name']!r} exists")
                await audit(c, who, "node pack added", p["name"], pack=p)
        return {"id": pid}

    @api.patch("/api/admin/node-packs/{pack_id}")
    async def patch_pack(pack_id: int, body: PackPatch, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            async with c.transaction():
                cur = await c.fetchrow("SELECT * FROM node_packs WHERE id = $1 FOR UPDATE", pack_id)
                if cur is None:
                    raise HTTPException(404, "no such node pack")
                merged = {k: cur[k] for k in ("name", "repo", "commit", "ref", "ui", "requirements_file", "constraints", "note")}
                merged["constraints"] = list(merged["constraints"] or [])
                merged.update({k: v for k, v in body.model_dump().items() if v is not None})
                p = check_pack(merged)
                await c.execute("UPDATE node_packs SET commit=$2, ref=$3, ui=$4, requirements_file=$5, constraints=$6, note=$7, "
                                "updated_at=now() WHERE id = $1", pack_id, p["commit"], p["ref"], p["ui"],
                                p["requirements_file"] or None, p["constraints"], p["note"])
                await audit(c, who, "node pack changed", p["name"], **{k: v for k, v in body.model_dump().items() if v is not None})
        return {"saved": True}

    @api.delete("/api/admin/node-packs/{pack_id}")
    async def delete_pack(pack_id: int, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            name = await c.fetchval("DELETE FROM node_packs WHERE id = $1 RETURNING name", pack_id)
            if name is None:
                raise HTTPException(404, "no such node pack")
            await audit(c, who, "node pack removed", name)
        return {"deleted": True}

    # ---- the UI worker container's first start -------------------------------------------------------------------
    def ui_paths() -> dict:
        home, shared, inst = s.ui_home, s.ui_shared, s.ui_instance
        return {"art": s.artifacts_dir, "local": f"{home}/local", "models": f"{home}/models", "temp": f"{home}/temp/{inst}",
                "input": f"{shared}/input", "output": f"{shared}/output", "user": f"{shared}/user"}

    async def ensure_ui_worker():
        """The compose deployment's UI worker, registered once. An operator's own UI worker (a host) is left alone, and
        one an administrator removed is not brought back."""
        url = s.ui_service.rstrip("/")
        async with db.conn() as c:
            uis = await c.fetch("SELECT id, base_url FROM workers WHERE kind = 'ui' AND deleted_at IS NULL")
            mine = [w for w in uis if w["base_url"].rstrip("/") == url]
            if mine:
                return mine[0]["id"]
            if uis:
                first.update(state="off", detail="another UI worker is registered — the container's is not used")
                return None
            if await c.fetchval("SELECT value FROM settings WHERE key = 'ui_container'"):
                first.update(state="off", detail="the UI worker container was removed in the dashboard — not added again")
                return None
            v = check_instance(InstanceIn(instance=s.ui_instance, role="ui", hosts=[re.sub(r"^https?://([^:/]+).*$", r"\1", url)],
                                          port=int(re.sub(r"^.*:(\d+)/?$", r"\1", url) if re.search(r":\d+/?$", url) else 80),
                                          paths=ui_paths(), restart_backend="http-agent",
                                          restart_config={"url": s.ui_agent, "token_file": s.ui_token}), s.keys_dir)
            async with c.transaction():
                name = "ui" if not await c.fetchval("SELECT 1 FROM workers WHERE name = 'ui' AND deleted_at IS NULL") else "ui-container"
                wid = await c.fetchval("INSERT INTO workers(name, base_url, kind) VALUES ($1, $2, 'ui') RETURNING id", name, url)
                await c.execute("INSERT INTO instances(worker_id, instance, role, platform, hosts, listen, port, paths, "
                                "restart_backend, restart_config) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)", wid, v["instance"],
                                v["role"], v["platform"], v["hosts"], v["listen"], v["port"], v["paths"], v["restart_backend"],
                                v["restart_config"])
                await c.execute("INSERT INTO settings(key, value, updated_by) VALUES ('ui_container', $1, $2) "
                                "ON CONFLICT (key) DO UPDATE SET value = excluded.value", {"worker_id": wid}, FIRST_START)
                await audit(c, FIRST_START, "UI worker container added", name, url=url)
            return wid

    def pick_template() -> dict | None:
        usable = [t for t in templates(s.artifacts_dir) if "ui" in t["roles"] and not [m for m in t["missing"]
                  if m.split("/", 1)[1] in (t["ui"].get("core"), t["ui"].get("venv"), *(t["ui"].get("nodes") or []))]]
        mine = [t for t in usable if t.get("recipe") == s.first_recipe] or usable
        return mine[-1] if mine else None

    async def start_ui(wid: int, tpl: dict):
        name, p = f"{tpl['template']}-ui", tpl["ui"]
        async with db.conn() as c:
            async with c.transaction():
                pid = await c.fetchval("SELECT id FROM profiles WHERE name = $1", name)
                if pid is None:
                    prof = check_profile({"name": name, "core": p["core"], "venv": p["venv"], "nodes": p.get("nodes") or [],
                                          "frontend": None, "flags": p.get("flags") or [], "env": p.get("env") or {},
                                          "note": f"UI worker · {tpl['template']} (first start)"})
                    pid = await c.fetchval("INSERT INTO profiles(name, core, venv, nodes, frontend, flags, env, note) "
                                           "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id", prof["name"], prof["core"],
                                           prof["venv"], prof["nodes"], None, prof["flags"], prof["env"], prof["note"])
                    await audit(c, FIRST_START, "profile created", name, profile=prof)
                inst = dict(await c.fetchrow("SELECT * FROM instances WHERE worker_id = $1", wid))
                rd.check(await profile_doc(c, pid), inst)
                await c.execute("UPDATE instances SET profile_id = $2, updated_at = now() WHERE worker_id = $1", wid, pid)
                await audit(c, FIRST_START, "assign", inst["instance"], to=pid, profile=name, restart=True)
        ok, detail = await be.make(inst["restart_backend"], inst["restart_config"], keys_dir=s.keys_dir).restart(inst["instance"])
        async with db.conn() as c:
            await audit(c, FIRST_START, "instance started", inst["instance"], ok=ok, detail=detail[-200:])
        return ok, detail

    async def first_start_step():
        wid = await ensure_ui_worker()
        if wid is None:
            return
        async with db.conn() as c:
            row = await c.fetchrow("SELECT i.profile_id, w.state FROM instances i JOIN workers w ON w.id = i.worker_id "
                                   "WHERE i.worker_id = $1", wid)
            rec = await c.fetchval("SELECT value FROM settings WHERE key = 'ui_first_build'")
        if row and row["profile_id"]:
            first.update(state="ready" if row["state"] == "online" else "starting",
                         detail="" if row["state"] == "online" else "ComfyUI is starting on the UI worker")
            return
        tpl = pick_template()
        if tpl:
            ok, detail = await start_ui(wid, tpl)
            first.update(state="starting" if ok else "error", detail=f"{tpl['template']}: " + ("starting" if ok else detail))
            return
        p = policy()
        if not s.builds_dir or not (p and p["up"]):
            first.update(state="waiting", detail="waiting for the builder to build ComfyUI for the UI worker")
            return
        st = None
        if rec and rec.get("id"):
            try:
                st = read_status(rec["id"])
            except HTTPException:
                st = None
        if st is None or (st.get("state") == "failed" and time.time() - rec.get("at", 0) > 600):
            if not (s.first_recipe in p.get("recipes", [])):
                first.update(state="error", detail=f"the builder has no recipe {s.first_recipe!r}")
                return
            rid = write_request({"kind": "build", "recipe": s.first_recipe, "roles": ["ui"], "nodes": [], "by": FIRST_START})
            async with db.conn() as c:
                await c.execute("INSERT INTO settings(key, value, updated_by) VALUES ('ui_first_build', $1, $2) ON CONFLICT (key) "
                                "DO UPDATE SET value = excluded.value, updated_at = now()", {"id": rid, "at": time.time()}, FIRST_START)
                await audit(c, FIRST_START, "build requested", s.first_recipe, request=rid, roles=["ui"])
            first.update(state="building", detail=f"building ComfyUI for the UI worker ({s.first_recipe}, CPU) — a few minutes")
            return
        state = st.get("state")
        first.update(state="building" if state in ("queued", "running") else "error",
                     detail=(f"building ComfyUI for the UI worker ({s.first_recipe}, CPU) — a few minutes"
                             if state in ("queued", "running") else f"the build failed: {st.get('error', '')} (tried again in 10 minutes)"))

    async def first_start_loop():
        if not (s.ui_service and s.ui_agent and s.artifacts_dir):
            return
        while True:
            try:
                await first_start_step()
            except Exception as e:                   # noqa: BLE001 — shown in the dashboard, tried again
                first.update(state="error", detail=f"{type(e).__name__}: {e}")
                log.warning("UI worker first start: %s: %s", type(e).__name__, e)
            await asyncio.sleep(60 if first["state"] in ("ready", "off") else 10)

    router.background.append(first_start_loop)
    router.first_start, router.first_start_step = first, first_start_step     # for the dashboard state and tests
    return api


async def resolve_hosts(names: list[str]) -> set[str]:
    """Host names in an instance's `hosts` (the compose UI worker is `ui`), as addresses — looked up when asked."""
    out = set()
    loop = asyncio.get_running_loop()
    for n in names:
        if re.fullmatch(r"[0-9.]+|[0-9a-fA-F:]+", n):
            continue
        try:
            for info in await loop.getaddrinfo(n, None, type=socket.SOCK_STREAM):
                out.add(info[4][0])
        except OSError:
            continue
    return out
