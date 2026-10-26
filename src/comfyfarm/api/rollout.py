"""Profiles, instances, the render endpoint and rollouts (docs/design/07, contract docs/contracts/comfy-upgrade-1.md).

Profiles that are in use are immutable (D-36): a change is made by copying the profile and rolling the copy out, so
what a worker runs only ever changes through a drained, verified, revertible step — never by editing a profile under
running workers.
"""
from __future__ import annotations

import json
import os
import re

import asyncpg
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from .. import launcher
from ..rollout import backends as be
from ..rollout import render as rd
from ..rollout.engine import audit, profile_doc
from .context import Ctx

NAME = launcher.NAME


class ProfileIn(BaseModel):
    name: str
    core: str
    venv: str
    nodes: list[str] = []
    frontend: str | None = None
    flags: list[str] = []
    env: dict[str, str] = {}
    note: str | None = None


class BuildIn(BaseModel):
    recipe: str


class ProfilePatch(BaseModel):
    name: str | None = None
    core: str | None = None
    venv: str | None = None
    nodes: list[str] | None = None
    frontend: str | None = None
    flags: list[str] | None = None
    env: dict[str, str] | None = None
    note: str | None = None


class InstanceIn(BaseModel):
    instance: str
    role: str = "gpu"
    platform: str = "posix"
    hosts: list[str] = []
    listen: str = "0.0.0.0"
    port: int
    paths: dict[str, str]
    restart_backend: str = "manual"
    restart_config: dict = {}
    # worker setup since 0.1.0-dev.19: its launcher takes any safe model folders — the render lists the farm's
    # (instances.model_folders); left out: the instance keeps what it had (none: the legacy list)
    open_folders: bool | None = None


class AssignIn(BaseModel):
    profile_id: int


class RolloutIn(BaseModel):
    profile_id: int
    worker_ids: list[int] = []
    group_id: int | None = None
    window: int = 1
    options: dict = {}


OPTION_KEYS = {"drain_timeout": (int, float), "start_timeout": (int, float), "observe_timeout": (int, float),
               "verify_timeout": (int, float), "fetch_timeout": (int, float), "smoke": bool,
               "restart": bool}          # restart even a worker that runs the target (after Set up again: new tools, env)


def iso(t):
    return t.isoformat() if t else None


def check_profile(p: dict) -> dict:
    for k in ("name", "core", "venv"):
        if not NAME.match(p.get(k) or ""):
            raise HTTPException(400, f"{k}: letters, digits and . _ - (max 63), starting with a letter or digit")
    for n in p.get("nodes") or []:
        if not NAME.match(n):
            raise HTTPException(400, f"node artifact name {n!r} is invalid")
    if p.get("frontend") and not NAME.match(p["frontend"]):
        raise HTTPException(400, "frontend artifact name is invalid")
    if bad := [f for f in p.get("flags") or [] if f in rd.MANAGED]:
        raise HTTPException(400, f"set by the instance, not the profile: {bad}")
    if bad := [f for f in p.get("flags") or [] if f.startswith("--") and f not in launcher.OPTIONS]:
        raise HTTPException(400, f"options the launcher refuses: {bad}")
    if bad := [why for k, v in (p.get("env") or {}).items() if (why := launcher.env_problem(k, v))]:
        hint = (" — a custom node's own setting is not a profile's: root sets it on the worker host "
                "(docs/guide/custom-nodes.md, \"Settings of a custom node\")"
                if any(not launcher.env_key_ok(k) for k in p.get("env") or {}) else "")
        raise HTTPException(400, f"environment the launcher refuses: {'; '.join(bad)}{hint}")
    return p


def check_instance(body: InstanceIn, keys_dir: str | None = None) -> dict:
    if not launcher.INSTANCE.match(body.instance):
        raise HTTPException(400, "instance: letters, digits and . _ - (max 63)")
    if body.role not in ("gpu", "ui") or body.platform not in ("posix", "windows"):
        raise HTTPException(400, "role must be gpu|ui, platform posix|windows")
    body.paths = {k: v for k, v in body.paths.items() if v or k not in rd.OPTIONAL_PATH_KEYS}   # left empty: none
    if missing := [k for k in rd.PATH_KEYS if not body.paths.get(k)]:
        raise HTTPException(400, f"paths missing: {missing}")
    win = body.platform == "windows"
    if bad := sorted(set(body.paths) - set(rd.PATH_KEYS) - set(rd.OPTIONAL_PATH_KEYS)):
        raise HTTPException(400, f"paths: unknown {bad}")
    for k in [k for k in rd.PATH_KEYS + rd.OPTIONAL_PATH_KEYS if body.paths.get(k)]:
        v = body.paths[k]
        if not (re.match(r"^[A-Za-z]:\\", v) if win else v.startswith("/")):
            raise HTTPException(400, f"paths.{k} must be absolute")
    if bad := rd.path_problems(body.paths, win):
        raise HTTPException(400, "; ".join(bad) + ": directories users write to must lie outside the artifact store "
                                 "and the local copies (custom nodes are imported from there)")
    if bad := rd.model_store_problem(body.paths, win):
        raise HTTPException(400, bad)
    if not 1 <= body.port <= 65535:
        raise HTTPException(400, "port out of range")
    try:
        be.make(body.restart_backend, body.restart_config, keys_dir=keys_dir)
    except be.BackendError as e:
        raise HTTPException(400, str(e))
    return body.model_dump()


def make_router(ctx: Ctx) -> APIRouter:
    api = APIRouter()
    router, db, settings = ctx.router, ctx.router.db, ctx.settings

    # ---- artifacts (optional: only if the router can see <ART>) ----------------------------------------------
    def manifest(kind: str, name: str) -> dict | None:
        if not settings.artifacts_dir:
            return None
        d = os.path.join(settings.artifacts_dir, kind, name)
        if not os.path.isfile(os.path.join(d, "READY")):
            return {"ready": False}
        try:
            with open(os.path.join(d, "manifest.json")) as f:
                return {"ready": True, **json.load(f)}
        except (OSError, ValueError):
            return {"ready": True}

    def artifact_problems(profile: dict, inst: dict, caps: dict) -> tuple[list[str], list[str]]:
        """(errors, warnings) — contract §9: never a venv for another platform; an accelerator that is known must
        match the worker's backend; unknown ones are only warned about."""
        if not settings.artifacts_dir:
            return [], ["the router cannot see the artifact store: READY files, platform and accelerator are unchecked"]
        errors, warnings = [], []
        need = [("core", profile["core"]), ("venv", profile["venv"])] + [("nodes", n) for n in profile["nodes"]]
        if profile.get("frontend"):
            need.append(("frontend", profile["frontend"]))
        for kind, name in need:
            if not manifest(kind, name)["ready"]:
                errors.append(f"{kind}/{name} is not published (no READY)")
        v = manifest("venv", profile["venv"])
        if v.get("ready"):
            if v.get("platform", "posix") != inst["platform"]:
                errors.append(f"venv {profile['venv']} is for {v.get('platform', 'posix')}, the worker is {inst['platform']}")
            acc, backend = v.get("accelerator"), (caps or {}).get("backend")
            if acc is None:
                warnings.append(f"venv {profile['venv']} does not declare its accelerator (unknown)")
            elif backend and acc != backend:
                errors.append(f"venv {profile['venv']} is built for {acc}, the worker runs {backend}")
        return errors, warnings

    @api.get("/api/admin/artifacts")
    async def artifacts(x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        if not settings.artifacts_dir:
            return {"available": False}
        out = {}
        for kind in launcher.KINDS:
            base = os.path.join(settings.artifacts_dir, kind)
            items = []
            for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
                m = manifest(kind, name) if NAME.match(name) else None
                if m and m["ready"]:
                    items.append({"name": name, **{k: m.get(k) for k in ("created", "accelerator", "arch", "platform", "class")}})
            out[kind] = sorted(items, key=lambda x: x.get("created") or "", reverse=True)
        return {"available": True, "artifacts": out}

    # ---- profiles ------------------------------------------------------------------------------------------------
    @api.get("/api/admin/profiles")
    async def list_profiles(x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            rows = await c.fetch("SELECT p.*, (SELECT count(*) FROM instances i WHERE i.profile_id = p.id) AS assigned "
                                 "FROM profiles p WHERE p.deleted_at IS NULL ORDER BY p.created_at DESC")
            kept = await kept_reasons(c)
        return [{**{k: (iso(r[k]) if k.endswith("_at") else r[k]) for k in r.keys() if k != "deleted_at"},
                 "kept": kept.get(r["id"])} for r in rows]

    async def kept_reasons(c) -> dict[int, dict]:
        """Why a profile cannot be deleted now (D-47), by profile id; the first reason found: it is assigned, a worker
        runs it (D-45: the render its command line matches — an assignment without a restart is not what runs), or an
        active rollout uses it (its target, or a profile a step goes back to). Everything else may go."""
        kept: dict[int, dict] = {}
        insts = await c.fetch("SELECT i.*, w.name AS worker FROM instances i JOIN workers w ON w.id = i.worker_id "
                              "WHERE w.deleted_at IS NULL ORDER BY w.name")
        for r in insts:
            if r["profile_id"] is not None:
                kept.setdefault(r["profile_id"], {"kind": "assigned", "worker": r["worker"]})
        for r in insts:
            pid = await running_profile(c, r["worker_id"], dict(r))
            if pid is not None:
                kept.setdefault(pid, {"kind": "running", "worker": r["worker"]})
        for r in await c.fetch("SELECT r.id, r.target_profile_id, s.from_profile_id, s.to_profile_id FROM rollouts r "
                               "LEFT JOIN rollout_steps s ON s.rollout_id = r.id "
                               "WHERE r.status IN ('running','aborting','interrupted') ORDER BY r.id"):
            for pid in (r["target_profile_id"], r["from_profile_id"], r["to_profile_id"]):
                if pid is not None:
                    kept.setdefault(pid, {"kind": "rollout", "rollout": r["id"]})
        return kept

    def kept_message(name: str, why: dict) -> str:
        return {"assigned": f"{name} is assigned to {why.get('worker')}",
                "running": f"{why.get('worker')} runs {name}",
                "rollout": f"rollout #{why.get('rollout')} uses {name}"}[why["kind"]]

    async def archive_profiles(c, ids: list[int], who: str) -> dict:
        """D-47: deleted = archived — the rollout history that names a profile keeps it; lists, assignments and new
        rollouts do not see it, and its name is free again."""
        kept = await kept_reasons(c)
        rows = {r["id"]: r["name"] for r in await c.fetch(
            "SELECT id, name FROM profiles WHERE id = ANY($1::bigint[]) AND deleted_at IS NULL FOR UPDATE", ids)}
        out = {"deleted": [], "kept": [], "missing": [i for i in ids if i not in rows]}
        for pid in ids:
            if pid not in rows:
                continue
            if pid in kept:
                out["kept"].append({"id": pid, "name": rows[pid], **kept[pid], "message": kept_message(rows[pid], kept[pid])})
                continue
            await c.execute("UPDATE profiles SET deleted_at = now() WHERE id = $1", pid)
            await audit(c, who, "profile deleted", rows[pid])
            out["deleted"].append({"id": pid, "name": rows[pid]})
        return out

    @api.post("/api/admin/profiles")
    async def create_profile(body: ProfileIn, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        p = check_profile(body.model_dump())
        async with db.conn() as c:
            async with c.transaction():
                try:
                    pid = await c.fetchval("INSERT INTO profiles(name, core, venv, nodes, frontend, flags, env, note) "
                                           "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id", p["name"], p["core"], p["venv"],
                                           p["nodes"], p["frontend"] or None, p["flags"], p["env"], p["note"])
                except asyncpg.UniqueViolationError:
                    raise HTTPException(409, f"profile {p['name']!r} exists")
                await audit(c, who, "profile created", p["name"], profile=p)
        return {"id": pid}

    @api.patch("/api/admin/profiles/{profile_id}")
    async def patch_profile(profile_id: int, body: ProfilePatch, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        patch = {k: v for k, v in body.model_dump().items() if v is not None}
        async with db.conn() as c:
            async with c.transaction():
                cur = await c.fetchrow("SELECT * FROM profiles WHERE id = $1 AND deleted_at IS NULL FOR UPDATE", profile_id)
                if cur is None:
                    raise HTTPException(404, "not found")
                in_use = await c.fetchval("SELECT count(*) FROM instances WHERE profile_id = $1", profile_id) or \
                    await c.fetchval("SELECT count(*) FROM rollout_steps s JOIN rollouts r ON r.id = s.rollout_id WHERE "
                                     "$1 IN (s.to_profile_id, s.from_profile_id) AND r.status IN ('running','aborting','interrupted')",
                                     profile_id)                     # the target, and the profile a step reverts to
                if in_use and set(patch) - {"note"}:
                    raise HTTPException(409, "this profile is in use: copy it and roll the copy out (only the note can change)")
                merged = check_profile({**{k: cur[k] for k in ("name", "core", "venv", "nodes", "frontend", "flags", "env", "note")},
                                        **patch})
                try:
                    await c.execute("UPDATE profiles SET name=$2, core=$3, venv=$4, nodes=$5, frontend=$6, flags=$7, env=$8, "
                                    "note=$9, updated_at=now() WHERE id=$1", profile_id, merged["name"], merged["core"],
                                    merged["venv"], merged["nodes"], merged["frontend"] or None, merged["flags"], merged["env"],
                                    merged["note"])
                except asyncpg.UniqueViolationError:
                    raise HTTPException(409, f"profile {merged['name']!r} exists")
                await audit(c, who, "profile changed", merged["name"], change=patch)
        return {"changed": True}

    @api.delete("/api/admin/profiles/{profile_id}")
    async def delete_profile(profile_id: int, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            async with c.transaction():
                out = await archive_profiles(c, [profile_id], who)
        if out["missing"]:
            raise HTTPException(404, "not found")
        if out["kept"]:
            k = out["kept"][0]
            raise HTTPException(409, {"message": k["message"], **{x: k[x] for x in ("kind", "worker", "rollout", "name") if x in k}})
        return {"deleted": True}

    @api.post("/api/admin/profiles/delete")
    async def delete_profiles(body: dict, x_comfyfarm_user: str | None = Header(default=None)):
        """Several at once (D-47): deletes what may go, says why each other one stays."""
        who = ctx.admin(x_comfyfarm_user)
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids) or len(ids) > 500:
            raise HTTPException(400, "ids: a list of profile ids (1..500)")
        async with db.conn() as c:
            async with c.transaction():
                return await archive_profiles(c, list(dict.fromkeys(ids)), who)

    # ---- instances -------------------------------------------------------------------------------------------
    async def inst_row(c, worker_id: int):
        r = await c.fetchrow("SELECT * FROM instances WHERE worker_id = $1", worker_id)
        return dict(r) if r else None

    async def in_active_rollout(c, worker_id: int) -> bool:
        return bool(await c.fetchval("SELECT 1 FROM rollout_steps s JOIN rollouts r ON r.id = s.rollout_id WHERE s.worker_id = $1 "
                                     "AND r.status IN ('running','aborting','interrupted')", worker_id))

    @api.get("/api/admin/instances")
    async def list_instances(x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            # explicit instance columns: `i.*` would bring a NULL worker_id for workers without an instance
            rows = await c.fetch("SELECT w.id AS worker_id, w.name AS worker, w.state, w.enabled, w.maintenance, w.capabilities, "
                                 "g.name AS group_name, i.instance, i.role, i.platform, i.hosts, i.listen, i.port, i.paths, "
                                 "i.restart_backend, i.restart_config, i.profile_id, i.model_folders, p.name AS profile_name FROM workers w "
                                 "LEFT JOIN worker_groups g ON g.id = w.group_id LEFT JOIN instances i ON i.worker_id = w.id "
                                 "LEFT JOIN profiles p ON p.id = i.profile_id WHERE w.deleted_at IS NULL ORDER BY w.name")
            profiles = {r["id"]: r for r in await c.fetch("SELECT * FROM profiles")}
        out = []
        for r in rows:
            caps = r["capabilities"] or {}
            d = {"worker_id": r["worker_id"], "worker": r["worker"], "group": r["group_name"], "state": r["state"],
                 "enabled": r["enabled"], "maintenance": r["maintenance"], "comfyui": caps.get("comfyui"),
                 "backend": caps.get("backend"), "instance": None}
            if r["instance"]:
                # every column the render reads (model_folders: field, dev.19 — without it the badge compared the
                # worker with the legacy folders and said `differs` for every host set up since)
                inst = {k: r[k] for k in ("instance", "role", "platform", "hosts", "listen", "port", "paths",
                                          "restart_backend", "restart_config", "profile_id", "model_folders")}
                render = rd.build(dict(profiles[r["profile_id"]]), inst) if r["profile_id"] else None
                d.update(instance=inst, profile=r["profile_name"], rev=render["rev"] if render else None,
                         badge=rd.badge(render, caps.get("argv")))
            out.append(d)
        return out

    @api.put("/api/admin/instances/{worker_id}")
    async def put_instance(worker_id: int, body: InstanceIn, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        v = check_instance(body, settings.keys_dir)
        async with db.conn() as c:
            async with c.transaction():
                if not await c.fetchval("SELECT 1 FROM workers WHERE id = $1 AND deleted_at IS NULL", worker_id):
                    raise HTTPException(404, "no such worker")
                if await in_active_rollout(c, worker_id):
                    raise HTTPException(409, "the worker is part of an active rollout")
                cur = await inst_row(c, worker_id)
                if body.open_folders is not None:
                    from .models import model_folders
                    v["model_folders"] = await model_folders(c) if body.open_folders else None
                else:
                    v["model_folders"] = cur["model_folders"] if cur else None
                if cur and cur["profile_id"]:
                    try:                                        # the new paths must still give a valid render
                        rd.check(await profile_doc(c, cur["profile_id"]), v)
                    except rd.BadProfile as e:
                        raise HTTPException(400, f"with the assigned profile the launcher would refuse: {e}")
                try:
                    await c.execute(
                        "INSERT INTO instances(worker_id, instance, role, platform, hosts, listen, port, paths, restart_backend, "
                        "restart_config, model_folders) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) ON CONFLICT (worker_id) DO UPDATE SET "
                        "instance=excluded.instance, role=excluded.role, platform=excluded.platform, hosts=excluded.hosts, "
                        "listen=excluded.listen, port=excluded.port, paths=excluded.paths, restart_backend=excluded.restart_backend, "
                        "restart_config=excluded.restart_config, model_folders=excluded.model_folders, updated_at=now()",
                        worker_id, v["instance"], v["role"], v["platform"], v["hosts"], v["listen"], v["port"], v["paths"],
                        v["restart_backend"], v["restart_config"], v["model_folders"])
                except asyncpg.UniqueViolationError:
                    raise HTTPException(409, f"instance name {v['instance']!r} is taken")
                await audit(c, who, "instance configured", v["instance"],       # restart_config holds references only
                            config=v, backend=v["restart_backend"])
        return {"saved": True}

    @api.delete("/api/admin/instances/{worker_id}")
    async def delete_instance(worker_id: int, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            async with c.transaction():
                if await in_active_rollout(c, worker_id):
                    raise HTTPException(409, "the worker is part of an active rollout")
                name = await c.fetchval("DELETE FROM instances WHERE worker_id = $1 RETURNING instance", worker_id)
                if name is None:
                    raise HTTPException(404, "not found")
                await audit(c, who, "instance removed", name)
        return {"deleted": True}

    async def check_assignment(c, worker_id: int, profile_id: int, force: bool = False):
        inst = await inst_row(c, worker_id)
        if inst is None:
            raise HTTPException(400, "configure the worker's instance first")
        prof = await profile_doc(c, profile_id)
        if prof is None or await c.fetchval("SELECT deleted_at IS NOT NULL FROM profiles WHERE id = $1", profile_id):
            raise HTTPException(404, "no such profile")             # a deleted (archived, D-47) one included
        try:
            render = rd.check(prof, inst)
        except rd.BadProfile as e:
            raise HTTPException(400, f"the launcher would refuse this render: {e}")
        caps = await c.fetchval("SELECT capabilities FROM workers WHERE id = $1", worker_id)
        errors, warnings = artifact_problems(prof, inst, caps)
        if errors and not force:
            raise HTTPException(400, "; ".join(errors))
        return inst, prof, render, warnings

    async def running_profile(c, worker_id: int, inst) -> int | None:
        """The profile a worker runs now, as far as can be told: the one whose render its argv matches (the badge's
        test) — its assignment first, then what its successful rollouts left it on (newest first), then any other
        profile. An assignment made without a restart is not what runs (field, dev.23: assign, then a rollout of the
        same profile — its revert had nowhere to go). Nothing matches (offline, never started): what its last
        successful rollout left it on, else its assignment."""
        caps = await c.fetchval("SELECT capabilities FROM workers WHERE id = $1", worker_id) or {}
        done = [r["to_profile_id"] for r in await c.fetch(
            "SELECT to_profile_id FROM rollout_steps WHERE worker_id = $1 AND status = 'done' ORDER BY id DESC LIMIT 10",
            worker_id)]
        if isinstance(caps.get("argv"), list):
            others = [r["id"] for r in await c.fetch("SELECT id FROM profiles ORDER BY id DESC")]
            seen = set()
            for pid in [inst["profile_id"], *done, *others]:
                if pid is None or pid in seen:
                    continue
                seen.add(pid)
                prof = await profile_doc(c, pid)
                try:
                    if prof is not None and rd.badge(rd.build(prof, inst), caps["argv"]) == "match":
                        return pid
                except Exception:                              # a profile that no longer renders cannot be running
                    continue
        return done[0] if done else inst["profile_id"]

    @api.post("/api/admin/instances/{worker_id}/assign")
    async def assign(worker_id: int, body: AssignIn, x_comfyfarm_user: str | None = Header(default=None)):
        """Set the assignment without restarting (first setup; the badge shows `differs` until the worker restarts).
        Upgrades of running workers go through a rollout."""
        who = ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            async with c.transaction():
                if await in_active_rollout(c, worker_id):
                    raise HTTPException(409, "the worker is part of an active rollout")
                inst, prof, render, warnings = await check_assignment(c, worker_id, body.profile_id)
                state = await c.fetchval("SELECT state FROM workers WHERE id = $1", worker_id)
                if state == "online" and prof["id"] != inst["profile_id"]:
                    warnings = [*warnings, "it keeps running its current profile until it restarts — to change what a "
                                "running worker runs, start a rollout: it checks the new profile and goes back on failure"]
                await c.execute("UPDATE instances SET profile_id = $2, updated_at = now() WHERE worker_id = $1", worker_id, prof["id"])
                await audit(c, who, "assign", inst["instance"], **{"from": inst["profile_id"], "to": prof["id"],
                                                                     "profile": prof["name"], "restart": False})
        return {"assigned": True, "rev": render["rev"], "warnings": warnings}

    @api.post("/api/admin/instances/{worker_id}/start")
    async def start_instance(worker_id: int, x_comfyfarm_user: str | None = Header(default=None)):
        """First start after setup and assignment (or after a stop): the restart backend starts the unit. Changing what
        a running worker runs still goes through a rollout."""
        who = ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            inst = await inst_row(c, worker_id)
            if inst is None or inst["profile_id"] is None:
                raise HTTPException(400, "configure the instance and assign a profile first")
            if await in_active_rollout(c, worker_id):
                raise HTTPException(409, "the worker is part of an active rollout")
            if await c.fetchval("SELECT state = 'online' FROM workers WHERE id = $1", worker_id):
                # a restart here would cut its running job: changing a running worker is a rollout (it drains first)
                raise HTTPException(409, "the worker is running — to change what it runs, start a rollout")
        try:
            backend = be.make(inst["restart_backend"], inst["restart_config"], keys_dir=settings.keys_dir)
        except be.BackendError as e:
            raise HTTPException(400, str(e))
        ok, detail = await backend.restart(inst["instance"])
        async with db.conn() as c:
            await audit(c, who, "instance started", inst["instance"], ok=ok, detail=detail[-200:])
        if not ok:
            raise HTTPException(502, f"the restart backend did not deliver: {detail[-300:]}")
        return {"started": True, "detail": detail}

    @api.post("/api/admin/instances/{worker_id}/check")
    async def check_host(worker_id: int, x_comfyfarm_user: str | None = Header(default=None)):
        """The host check (docs/design/14), asked through the instance's agent: read-only."""
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            inst = await inst_row(c, worker_id)
        if inst is None:
            raise HTTPException(404, "no instance")
        try:
            out = await be.make(inst["restart_backend"], inst["restart_config"], keys_dir=settings.keys_dir).check(inst["instance"])
        except be.BackendError as e:
            raise HTTPException(502, str(e))
        if out is None:
            raise HTTPException(400, f"the {inst['restart_backend']} backend has no agent to run the host check")
        return {"instance": inst["instance"], "check": out}

    async def builder(worker_id: int):
        async with db.conn() as c:
            inst = await inst_row(c, worker_id)
        if inst is None:
            raise HTTPException(404, "no instance")
        return inst, be.make(inst["restart_backend"], inst["restart_config"], keys_dir=settings.keys_dir)

    @api.post("/api/admin/instances/{worker_id}/build")
    async def build(worker_id: int, body: BuildIn, x_comfyfarm_user: str | None = Header(default=None)):
        """A recipe build on that host (only where its setup allowed it): the router names an installed recipe; the
        host runs it as its build account. Progress: GET the same path."""
        who = ctx.admin(x_comfyfarm_user)
        inst, b = await builder(worker_id)
        try:
            out = await b.build(body.recipe)
        except be.BackendError as e:
            raise HTTPException(502, str(e))
        if out is None:
            raise HTTPException(400, f"the {inst['restart_backend']} backend has no agent to ask for a build")
        async with db.conn() as c:
            await audit(c, who, "build requested", inst["instance"], recipe=body.recipe)
        return out

    @api.get("/api/admin/instances/{worker_id}/build")
    async def build_state(worker_id: int, recipe: str, x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        inst, b = await builder(worker_id)
        try:
            out = await b.build(recipe, state=True)
        except be.BackendError as e:
            raise HTTPException(502, str(e))
        if out is None:
            raise HTTPException(400, f"the {inst['restart_backend']} backend has no agent")
        return out

    @api.get("/api/admin/instances/{worker_id}/render")
    async def preview_render(worker_id: int, profile_id: int | None = None, x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            inst = await inst_row(c, worker_id)
            if inst is None:
                raise HTTPException(404, "no instance")
            pid = profile_id or inst["profile_id"]
            if pid is None:
                raise HTTPException(404, "nothing assigned")
            prof = await profile_doc(c, pid)
        return rd.build(prof, inst)

    # ---- the launcher's call (contract §3) -------------------------------------------------------------------
    @api.get("/api/worker_profile")
    async def worker_profile(instance: str, request: Request):
        """No user identity: the caller is a launcher. Only the instance's own host gets its render (403 otherwise;
        the launcher then refuses to start instead of falling back to a cached render)."""
        async with db.conn() as c:
            inst = await c.fetchrow("SELECT * FROM instances WHERE instance = $1", instance)
            if inst is None:
                raise HTTPException(404, "no such instance")
            caller = request.client.host if request.client else ""
            if caller not in inst["hosts"]:
                from .builds import resolve_hosts          # a name in hosts (the compose UI worker is `ui`): its addresses
                if caller not in await resolve_hosts(list(inst["hosts"])):
                    raise HTTPException(403, "not this instance's host")
            if inst["profile_id"] is None:
                raise HTTPException(404, "nothing assigned")
            prof = await profile_doc(c, inst["profile_id"])
        return rd.build(prof, dict(inst))

    # ---- rollouts --------------------------------------------------------------------------------------------
    def step_doc(r) -> dict:
        return {"id": r["id"], "worker_id": r["worker_id"], "worker": r["worker"], "position": r["position"],
                "from": r["from_name"], "to": r["to_name"], "status": r["status"], "detail": r["detail"], "error": r["error"],
                "backend": r["restart_backend"], "instance": r["instance"],
                "started_at": iso(r["started_at"]), "finished_at": iso(r["finished_at"])}

    STEPS = ("SELECT s.*, w.name AS worker, pf.name AS from_name, pt.name AS to_name, i.restart_backend, i.instance "
             "FROM rollout_steps s JOIN workers w ON w.id = s.worker_id LEFT JOIN profiles pf ON pf.id = s.from_profile_id "
             "JOIN profiles pt ON pt.id = s.to_profile_id LEFT JOIN instances i ON i.worker_id = s.worker_id ")

    def rollout_doc(r, steps) -> dict:
        return {"id": r["id"], "status": r["status"], "profile": r["profile_name"], "profile_id": r["target_profile_id"],
                "window": r["window_size"], "options": r["options"], "created_by": r["created_by"],
                "created_at": iso(r["created_at"]), "finished_at": iso(r["finished_at"]), "error": r["error"],
                "steps": [step_doc(s) for s in steps]}

    @api.post("/api/admin/rollouts")
    async def start_rollout(body: RolloutIn, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        if not 1 <= body.window <= 16:
            raise HTTPException(400, "window must be 1..16")
        for k, v in body.options.items():
            if k not in OPTION_KEYS or not isinstance(v, OPTION_KEYS[k]) or (v is not True and v is not False and v < 0):
                raise HTTPException(400, f"bad option {k!r}")
        async with db.conn() as c:
            async with c.transaction():
                ids = list(dict.fromkeys(body.worker_ids))
                if body.group_id is not None:
                    ids += [r["id"] for r in await c.fetch("SELECT w.id FROM workers w JOIN instances i ON i.worker_id = w.id "
                                                           "WHERE w.group_id = $1 AND w.deleted_at IS NULL AND w.enabled "
                                                           "ORDER BY w.name", body.group_id) if r["id"] not in ids]
                if not ids:
                    raise HTTPException(400, "no workers selected")
                warnings, plan = [], []
                for wid in ids:
                    name = await c.fetchval("SELECT name FROM workers WHERE id = $1 AND deleted_at IS NULL", wid)
                    if name is None:
                        raise HTTPException(404, f"no worker {wid}")
                    try:
                        inst, prof, _render, warn = await check_assignment(c, wid, body.profile_id)
                    except HTTPException as e:
                        raise HTTPException(e.status_code, f"{name}: {e.detail}")
                    warnings += [f"{name}: {x}" for x in warn]
                    plan.append((wid, await running_profile(c, wid, inst)))    # what a failure goes back to
                try:
                    async with c.transaction():
                        rid = await c.fetchval("INSERT INTO rollouts(target_profile_id, window_size, options, created_by) "
                                               "VALUES ($1, $2, $3, $4) RETURNING id", body.profile_id, body.window, body.options, who)
                except asyncpg.UniqueViolationError:
                    raise HTTPException(409, "another rollout is active")
                await c.executemany("INSERT INTO rollout_steps(rollout_id, worker_id, position, from_profile_id, to_profile_id) "
                                    "VALUES ($1, $2, $3, $4, $5)",
                                    [(rid, wid, i, frm, body.profile_id) for i, (wid, frm) in enumerate(plan)])
                await audit(c, who, "rollout started", f"rollout #{rid}", profile_id=body.profile_id, workers=ids,
                            window=body.window, options=body.options)
        return {"id": rid, "warnings": sorted(set(warnings))}

    @api.get("/api/admin/rollouts")
    async def list_rollouts(x_comfyfarm_user: str | None = Header(default=None), limit: int = 20):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            rows = await c.fetch("SELECT r.*, p.name AS profile_name FROM rollouts r JOIN profiles p ON p.id = r.target_profile_id "
                                 "ORDER BY r.id DESC LIMIT $1", min(max(limit, 1), 200))
            steps = await c.fetch(STEPS + "WHERE s.rollout_id = ANY($1) ORDER BY s.position", [r["id"] for r in rows])
        by = {}
        for s in steps:
            by.setdefault(s["rollout_id"], []).append(s)
        return [rollout_doc(r, by.get(r["id"], [])) for r in rows]

    @api.get("/api/admin/rollouts/{rid}")
    async def get_rollout(rid: int, x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            r = await c.fetchrow("SELECT r.*, p.name AS profile_name FROM rollouts r JOIN profiles p ON p.id = r.target_profile_id "
                                 "WHERE r.id = $1", rid)
            if r is None:
                raise HTTPException(404, "not found")
            steps = await c.fetch(STEPS + "WHERE s.rollout_id = $1 ORDER BY s.position", rid)
        return rollout_doc(r, steps)

    @api.post("/api/admin/rollouts/{rid}/abort")
    async def abort_rollout(rid: int, x_comfyfarm_user: str | None = Header(default=None)):
        """Running: no new steps; a worker still draining goes back to work; restarts already under way finish.
        Interrupted (nobody drives it): settled here — draining workers go back to work, workers caught mid-restart
        stay in maintenance and are marked failed for a human to look at."""
        who = ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            async with c.transaction():
                st = await c.fetchval("SELECT status FROM rollouts WHERE id = $1 FOR UPDATE", rid)
                if st is None:
                    raise HTTPException(404, "not found")
                if st == "running":
                    await c.execute("UPDATE rollouts SET status = 'aborting' WHERE id = $1", rid)
                elif st == "interrupted":
                    for s in await c.fetch("SELECT s.*, w.name FROM rollout_steps s JOIN workers w ON w.id = s.worker_id "
                                           "WHERE s.rollout_id = $1 AND s.status NOT IN ('pending','done','failed','reverted',"
                                           "'revert_failed','skipped')", rid):
                        if s["status"] == "fetching":          # only copies ran: nothing about the worker changed
                            await c.execute("UPDATE rollout_steps SET status = 'skipped', detail = 'aborted', "
                                            "finished_at = now() WHERE id = $1", s["id"])
                        elif s["status"] == "draining":
                            if not s["was_maintenance"]:
                                await c.execute("UPDATE workers SET maintenance = false WHERE id = $1", s["worker_id"])
                                await audit(c, who, "maintenance off", s["name"], rollout=rid)
                            await c.execute("UPDATE rollout_steps SET status = 'skipped', detail = 'aborted', finished_at = now() "
                                            "WHERE id = $1", s["id"])
                        else:
                            await c.execute("UPDATE rollout_steps SET status = 'failed', finished_at = now(), error = "
                                            "'interrupted during ' || status || '; left in maintenance — check this worker' "
                                            "WHERE id = $1", s["id"])
                    await c.execute("UPDATE rollout_steps SET status = 'skipped', detail = 'aborted', finished_at = now() "
                                    "WHERE rollout_id = $1 AND status = 'pending'", rid)
                    await c.execute("UPDATE rollouts SET status = 'aborted', finished_at = now() WHERE id = $1", rid)
                elif st != "aborting":
                    raise HTTPException(409, f"rollout is {st}")
                await audit(c, who, "rollout abort", f"rollout #{rid}")
        router.dispatcher.wake.set()
        return {"status": "aborting" if st == "running" else "aborted"}

    @api.post("/api/admin/rollouts/{rid}/resume")
    async def resume_rollout(rid: int, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            async with c.transaction():
                done = await c.fetchval("UPDATE rollouts SET status = 'running', error = NULL WHERE id = $1 AND status = 'interrupted' "
                                        "RETURNING id", rid)
                if done is None:
                    raise HTTPException(409, "only an interrupted rollout can be resumed")
                await audit(c, who, "rollout resumed", f"rollout #{rid}")
        return {"status": "running"}

    @api.get("/api/admin/audit")
    async def audit_log(x_comfyfarm_user: str | None = Header(default=None), limit: int = 100, offset: int = 0):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            rows = await c.fetch("SELECT * FROM audit_log ORDER BY id DESC OFFSET $1 LIMIT $2", max(offset, 0), min(max(limit, 1), 500))
        return [{"at": iso(r["at"]), "actor": r["actor"], "action": r["action"], "target": r["target"], "detail": r["detail"]}
                for r in rows]

    @api.get("/api/admin/audit/page")
    async def audit_page(x_comfyfarm_user: str | None = Header(default=None), offset: int = 0, limit: int = 20,
                         upto: int | None = None):
        """A page of the audit log and how many entries there are. `upto`: the newest entry when the reader left the
        first page — older pages stay put while new entries come in (the first page, without it, shows them)."""
        ctx.admin(x_comfyfarm_user)
        offset, limit = max(offset, 0), min(max(limit, 1), 200)
        async with db.conn() as c:
            top = upto if upto is not None else await c.fetchval("SELECT coalesce(max(id), 0) FROM audit_log")
            total = await c.fetchval("SELECT count(*) FROM audit_log WHERE id <= $1", top)
            rows = await c.fetch("SELECT * FROM audit_log WHERE id <= $1 ORDER BY id DESC OFFSET $2 LIMIT $3", top, offset, limit)
        return {"items": [{"id": r["id"], "at": iso(r["at"]), "actor": r["actor"], "action": r["action"], "target": r["target"],
                           "detail": r["detail"]} for r in rows], "total": total, "offset": offset, "upto": top}

    return api
