"""Models from the dashboard (docs/design/17). The router never downloads and never writes the model store: it asks the
builder for a model (builder.py, kind "model") and follows what it writes back; it hands GPU hosts the builder's signed
entries and the store's files as they are (the host checks both against the builder's key it pinned at setup); and it
asks each copy-mode host's agent for the models its group uses, in the background — no restart, no drain."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import urllib.parse

import asyncpg
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import fetch
from ..builder import HF_PATH, HF_REPO, REF
from ..db import Busy, BusyLog
from ..rollout import backends as be
from ..rollout.engine import audit
from .artifacts import worker_host
from .builds import builder_status, read_policy, write_builder_request
from .context import Ctx

log = logging.getLogger(__name__)
HF_URL = re.compile(r"^https://(?:huggingface\.co|hf\.co)/(?P<repo>[^/?#]+/[^/?#]+)/(?:blob|resolve)/(?P<ref>[^/?#]+)/"
                    r"(?P<path>[^?#]+)(?:[?#].*)?$")
TICK = 5.0                          # builder states
COPIES_EVERY = 30.0                 # asking hosts how their copies stand
RETRY_FAILED = 600.0                # a copy that failed is asked for again after this long
MAX_BATCH = 200


class ModelIn(BaseModel):
    url: str | None = None              # https://huggingface.co/<repo>/blob/<ref>/<path> (as copied from the site), or
    repo: str | None = None
    path: str | None = None
    ref: str | None = None              # a branch, tag or commit; pinned to the commit it names when it is fetched
    folder: str
    filename: str | None = None         # the name in ComfyUI's folder; the file's own name by default
    note: str | None = None


class ModelsIn(BaseModel):
    models: list[ModelIn]
    groups: list[int] = []


class ModelPatch(BaseModel):
    groups: list[int] | None = None
    note: str | None = None


class GroupsBulk(BaseModel):
    models: list[int]
    add: list[int] = []                 # groups the models are given
    remove: list[int] = []              # groups they are taken out of (their copies stay, as for one)


class InboxScan(BaseModel):
    groups: list[int] = []              # given to every model this scan takes in


class FolderIn(BaseModel):
    name: str


FOLDERS = "model_folders"               # settings key: the farm's own model folders (a custom node's), {"custom": [...]}


async def model_folders(c) -> list[str]:
    """ComfyUI's model folders and the farm's own: what models may go in, and what hosts set up since 0.1.0-dev.19
    list in ComfyUI (instances.model_folders)."""
    v = await c.fetchval("SELECT value FROM settings WHERE key = $1", FOLDERS) or {}
    return list(fetch.MODEL_FOLDERS) + [f for f in v.get("custom") or [] if fetch.folder_ok(f) and f not in fetch.MODEL_FOLDERS]


async def set_custom_folders(c, custom: list[str], who: str) -> None:
    """The farm's own folders, and with them every instance that lists folders (its render changes: ComfyUI on it sees
    a new folder after its next start)."""
    await c.execute("INSERT INTO settings(key, value, updated_by) VALUES ($1, $2, $3) ON CONFLICT (key) DO UPDATE SET "
                    "value = excluded.value, updated_by = excluded.updated_by, updated_at = now()",
                    FOLDERS, {"custom": custom}, who)
    await c.execute("UPDATE instances SET model_folders = $1, updated_at = now() WHERE model_folders IS NOT NULL",
                    list(fetch.MODEL_FOLDERS) + custom)


def check_model(m: ModelIn, formats: list[str] | None, folders=fetch.MODEL_FOLDERS) -> dict:
    """What an operator typed or pasted → repo, path, ref, folder, filename; refused here what the builder would."""
    repo, path, ref = m.repo, m.path, m.ref
    if m.url:
        u = HF_URL.match(m.url.strip())
        if not u:
            raise ValueError(f"not a Hugging Face file link: {m.url.strip()[:120]!r} "
                             "(https://huggingface.co/<owner>/<repo>/blob/<branch>/<file>)")
        repo, ref, path = u["repo"], urllib.parse.unquote(u["ref"]), urllib.parse.unquote(u["path"])
    repo, path, ref = (repo or "").strip(), (path or "").strip(), (ref or "main").strip()
    if not HF_REPO.match(repo):
        raise ValueError(f"repository: <owner>/<name> on Hugging Face, not {repo!r}")
    if not HF_PATH.match(path):
        raise ValueError(f"file: a path in the repository, not {path!r}")
    if not REF.match(ref):
        raise ValueError(f"revision: a branch, tag or commit, not {ref!r}")
    if m.folder not in folders:
        raise ValueError(f"folder: one of {', '.join(folders)} (System → Models → Folders adds one)")
    filename = (m.filename or "").strip() or path.rsplit("/", 1)[-1]
    if not fetch.MODEL_FILE.match(filename):
        raise ValueError(f"file name: letters, digits, spaces and . _ + ( ) - — not {filename!r}")
    if formats is not None and not (filename.lower().endswith(tuple(formats)) and path.lower().endswith(tuple(formats))):
        raise ValueError(f"{filename}: only {', '.join(formats)} — other formats can run code when ComfyUI loads them "
                         "(docs/guide/models.md)")
    return {"repo": repo, "path": path, "ref": ref, "folder": m.folder, "filename": filename,
            "note": (m.note or "").strip() or None}


def row_out(r, groups: list[int]) -> dict:
    return {k: r[k] for k in ("id", "folder", "filename", "source", "repo", "path", "ref", "revision", "size", "sha256", "key",
                              "state", "bytes", "error", "note", "created_by")} | {
        "created_at": r["created_at"].isoformat(), "groups": groups}


INBOX = "model_inbox"                   # settings key: the last scan of the inbox the builder was asked for


def make_router(ctx: Ctx) -> APIRouter:
    api = APIRouter()
    router, db, s = ctx.router, ctx.router.db, ctx.settings
    copies: dict[int, dict] = {}            # worker id → what its host said last (the dashboard's per-worker status)
    asked: dict[tuple[int, str], float] = {}  # (worker id, key) → when its copy was last asked for
    wake = asyncio.Event()                  # a model added, ready or given to a group: ask the hosts now
    router.models_wake = wake               # uploads (api/uploads.py) wake it too

    def policy() -> dict | None:
        return read_policy(s.builds_dir)

    def need_builder() -> dict:
        if not s.builds_dir:
            raise HTTPException(400, "no control-plane builder here (the compose deployment's builder service)")
        p = policy()
        if not p or not p["up"]:
            raise HTTPException(503, "the builder is not running (docker compose ps builder; its log says why)")
        if not p.get("models"):
            raise HTTPException(400, "the builder has no model store (COMFYFARM_MODELS, docs/guide/models.md)")
        return p

    async def check_groups(c, groups: list[int]) -> None:
        if groups:
            known = {r["id"] for r in await c.fetch("SELECT id FROM worker_groups WHERE id = ANY($1)", groups)}
            if missing := [g for g in groups if g not in known]:
                raise HTTPException(400, f"no group {missing[0]}")

    # ---- the administrator's ---------------------------------------------------------------------------------------
    @api.get("/api/admin/models")
    async def list_models(x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        p = policy() if s.builds_dir else None
        async with db.conn() as c:
            rows = await c.fetch("SELECT * FROM models WHERE state <> 'deleted' ORDER BY folder, filename")
            links = await c.fetch("SELECT model_id, group_id FROM group_models")
            folders = await model_folders(c)
            workers = await c.fetch("SELECT w.id, w.name, w.group_id, w.state, "
                                    "coalesce(i.paths->>'model_store', '') <> '' AS copies, i.model_folders IS NULL AS legacy "
                                    "FROM workers w LEFT JOIN instances i ON i.worker_id = w.id "
                                    "WHERE w.kind = 'gpu' AND w.deleted_at IS NULL ORDER BY w.name")
        by_model: dict[int, list[int]] = {}
        for x in links:
            by_model.setdefault(x["model_id"], []).append(x["group_id"])
        async with db.conn() as c:
            inbox = await inbox_state(c)
        return {"inbox": inbox, "uploads": bool(s.models_dir and os.access(os.path.join(s.models_dir, ".uploads"), os.W_OK)),
                "builder": {"configured": bool(s.builds_dir), "up": bool(p and p["up"]), "models": bool(p and p.get("models")),
                            "inbox": (p or {}).get("inbox"), "archives": bool(p and p.get("node_archives")),
                            "formats": (p or {}).get("model_formats"), "hf_token": bool(p and p.get("hf_token")),
                            "store": bool(s.models_dir), "folders": folders,
                            "custom_folders": [f for f in folders if f not in fetch.MODEL_FOLDERS]},
                "models": [row_out(r, sorted(by_model.get(r["id"], []))) for r in rows],
                "workers": [{"id": w["id"], "name": w["name"], "group_id": w["group_id"], "state": w["state"],
                             "copies": bool(w["copies"]), "legacy": bool(w["copies"] and w["legacy"]),
                             **copies.get(w["id"], {})} for w in workers]}

    # ---- the farm's own model folders ----------------------------------------------------------------------------
    @api.post("/api/admin/models/folders")
    async def add_folder(body: FolderIn, x_comfyfarm_user: str | None = Header(default=None)):
        """A folder a custom node looks its models up in (folder_paths): models may go in it, and ComfyUI on hosts set
        up since 0.1.0-dev.19 lists it after its next start."""
        who = ctx.admin(x_comfyfarm_user)
        name = body.name.strip()
        if not fetch.folder_ok(name):
            raise HTTPException(400, "folder: lowercase letters, digits and _ . - (as the custom node names it); not "
                                     + ", ".join(fetch.RESERVED_FOLDERS))
        async with db.conn() as c:
            async with c.transaction():
                folders = await model_folders(c)
                if name in folders:
                    raise HTTPException(409, f"{name} is a folder already")
                await set_custom_folders(c, [f for f in folders if f not in fetch.MODEL_FOLDERS] + [name], who)
                await audit(c, who, "model folder added", name)
        return {"folders": folders + [name]}

    @api.delete("/api/admin/models/folders/{name}")
    async def remove_folder(name: str, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            async with c.transaction():
                folders = await model_folders(c)
                if name in fetch.MODEL_FOLDERS:
                    raise HTTPException(409, f"{name} is one of ComfyUI's own folders")
                if name not in folders:
                    raise HTTPException(404, "no such folder")
                if n := await c.fetchval("SELECT count(*) FROM models WHERE folder = $1 AND state <> 'deleted'", name):
                    raise HTTPException(409, f"{n} model(s) are in {name} — delete them first")
                await set_custom_folders(c, [f for f in folders if f not in fetch.MODEL_FOLDERS and f != name], who)
                await audit(c, who, "model folder removed", name)
        return {"deleted": True}

    @api.post("/api/admin/models")
    async def add_models(body: ModelsIn, x_comfyfarm_user: str | None = Header(default=None)):
        """One model or a pasted list: all of it is checked first, then each goes to the builder as its own request."""
        who = ctx.admin(x_comfyfarm_user)
        p = need_builder()
        if not body.models or len(body.models) > MAX_BATCH:
            raise HTTPException(400, f"1 to {MAX_BATCH} models at a time")
        items, seen = [], set()
        async with db.conn() as c:
            folders = await model_folders(c)
        for i, m in enumerate(body.models, 1):
            try:
                it = check_model(m, p.get("model_formats"), folders)
            except ValueError as e:
                raise HTTPException(400, f"line {i}: {e}" if len(body.models) > 1 else str(e))
            if (it["folder"], it["filename"]) in seen:
                raise HTTPException(400, f"line {i}: {it['folder']}/{it['filename']} twice")
            seen.add((it["folder"], it["filename"]))
            items.append(it)
        ids = []
        async with db.conn() as c:
            async with c.transaction():
                await check_groups(c, body.groups)
                for it in items:
                    try:
                        mid = await c.fetchval(
                            "INSERT INTO models(folder, filename, repo, path, ref, note, created_by) "
                            "VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id", it["folder"], it["filename"], it["repo"],
                            it["path"], it["ref"], it["note"], who)
                    except asyncpg.UniqueViolationError:
                        raise HTTPException(409, f"{it['folder']}/{it['filename']} is in the list already")
                    for g in body.groups:
                        await c.execute("INSERT INTO group_models(group_id, model_id) VALUES ($1, $2)", g, mid)
                    ids.append(mid)
                # requests last: a refused batch leaves the builder nothing to fetch
                for mid, it in zip(ids, items):
                    rid = write_builder_request(s.builds_dir, {"kind": "model", **{k: it[k] for k in (
                        "repo", "path", "ref", "folder", "filename")}, "by": who})
                    await c.execute("UPDATE models SET request = $2 WHERE id = $1", mid, rid)
                    await audit(c, who, "model added", f"{it['folder']}/{it['filename']}", repo=it["repo"],
                                path=it["path"], ref=it["ref"], groups=body.groups, request=rid)
        wake.set()
        return {"ids": ids}

    @api.patch("/api/admin/models/{model_id}")
    async def patch_model(model_id: int, body: ModelPatch, x_comfyfarm_user: str | None = Header(default=None)):
        """Which groups use it. Added to one, its workers get a copy in the background; taken out of one, that
        group's workers drop theirs (docs/design/17 phase 2 — hosts set up since 0.1.0-dev.19)."""
        who = ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            async with c.transaction():
                r = await c.fetchrow("SELECT folder, filename, state FROM models WHERE id = $1 FOR UPDATE", model_id)
                if r is None or r["state"] == "deleted":
                    raise HTTPException(404, "no such model")
                if body.groups and r["state"] == "deleting":
                    raise HTTPException(409, "it is being deleted from the farm")
                if body.groups is not None:
                    await check_groups(c, body.groups)
                    await c.execute("DELETE FROM group_models WHERE model_id = $1 AND NOT (group_id = ANY($2))", model_id, body.groups)
                    for g in body.groups:
                        await c.execute("INSERT INTO group_models(group_id, model_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                                        g, model_id)
                if body.note is not None:
                    await c.execute("UPDATE models SET note = $2 WHERE id = $1", model_id, body.note.strip() or None)
                await audit(c, who, "model changed", f"{r['folder']}/{r['filename']}",
                            **{k: v for k, v in body.model_dump().items() if v is not None})
        wake.set()
        return {"saved": True}

    @api.post("/api/admin/models/groups")
    async def bulk_groups(body: GroupsBulk, x_comfyfarm_user: str | None = Header(default=None)):
        """Several models into groups, or out of them, at once (field: an inbox of many files, each its own Groups
        dialog). Taken out of a group, its workers drop their copies, as for one."""
        who = ctx.admin(x_comfyfarm_user)
        if not body.models or len(body.models) > 1000:
            raise HTTPException(400, "1 to 1000 models")
        if not body.add and not body.remove:
            raise HTTPException(400, "add or remove: at least one group")
        if set(body.add) & set(body.remove):
            raise HTTPException(400, "a group to add and remove at once")
        async with db.conn() as c:
            async with c.transaction():
                await check_groups(c, sorted(set(body.add) | set(body.remove)))
                found = [r["id"] for r in await c.fetch("SELECT id FROM models WHERE id = ANY($1) AND state NOT IN "
                                                        "('deleting', 'deleted') FOR UPDATE", body.models)]
                if missing := sorted(set(body.models) - set(found)):
                    raise HTTPException(404, f"no model {missing[0]}")
                added = await c.fetchval(
                    "WITH x AS (INSERT INTO group_models(group_id, model_id) SELECT g, m FROM unnest($1::bigint[]) g, "
                    "unnest($2::bigint[]) m ON CONFLICT DO NOTHING RETURNING 1) SELECT count(*) FROM x", body.add, found)
                removed = await c.fetchval(
                    "WITH x AS (DELETE FROM group_models WHERE group_id = ANY($1) AND model_id = ANY($2) RETURNING 1) "
                    "SELECT count(*) FROM x", body.remove, found)
                await audit(c, who, "models' groups changed", f"{len(found)} model(s)", models=found, add=body.add,
                            remove=body.remove)
        wake.set()
        return {"models": len(found), "added": added, "removed": removed}

    @api.post("/api/admin/models/{model_id}/retry")
    async def retry_model(model_id: int, x_comfyfarm_user: str | None = Header(default=None)):
        """A download that failed, asked for again: the builder goes on from what it had (a gated model whose terms
        were accepted since, a broken connection)."""
        who = ctx.admin(x_comfyfarm_user)
        need_builder()
        async with db.conn() as c:
            async with c.transaction():
                r = await c.fetchrow("SELECT * FROM models WHERE id = $1 FOR UPDATE", model_id)
                if r is None:
                    raise HTTPException(404, "no such model")
                if r["state"] != "failed":
                    raise HTTPException(409, f"it is {r['state']}, not failed")
                if r["source"] == "inbox":
                    raise HTTPException(409, "a file from the inbox: put it there again and scan the inbox")
                if r["source"] == "upload":
                    if not os.path.isfile(os.path.join(s.models_dir or "", ".uploads", f"{r['upload']}.part")):
                        raise HTTPException(409, "the uploaded file is gone — remove this one and upload it again")
                    req = {"kind": "model-file", "upload": str(r["upload"]), "folder": r["folder"], "filename": r["filename"],
                           "size": r["size"]}
                else:
                    req = {"kind": "model", **{k: r[k] for k in ("repo", "path", "ref", "folder", "filename")}}
                rid = write_builder_request(s.builds_dir, {**req, "by": who})
                await c.execute("UPDATE models SET state = 'queued', error = NULL, request = $2 WHERE id = $1", model_id, rid)
                await audit(c, who, "model retried", f"{r['folder']}/{r['filename']}", request=rid)
        wake.set()
        return {"id": rid}

    @api.delete("/api/admin/models/{model_id}")
    async def delete_model(model_id: int, x_comfyfarm_user: str | None = Header(default=None)):
        """One that never arrived (a typo, a gated model) goes from the list. One in the store is deleted from the
        farm (docs/design/17 phase 2): out of its groups, the builder takes it out of the store, and the hosts drop
        their copies. The site's own models directory is never touched."""
        who = ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            async with c.transaction():
                r = await c.fetchrow("SELECT folder, filename, state, upload, key FROM models WHERE id = $1 FOR UPDATE",
                                     model_id)
                if r is None or r["state"] == "deleted":
                    raise HTTPException(404, "no such model")
                if r["state"] in ("queued", "downloading"):
                    raise HTTPException(409, f"it is {r['state']} — delete it once it is in the store or has failed")
                if r["state"] in ("ready", "deleting"):
                    need_builder()
                    rid = write_builder_request(s.builds_dir, {"kind": "model-remove", "key": r["key"], "by": who})
                    await c.execute("DELETE FROM group_models WHERE model_id = $1", model_id)
                    await c.execute("UPDATE models SET state = 'deleting', request = $2, error = NULL WHERE id = $1",
                                    model_id, rid)
                    await audit(c, who, "model deleted", f"{r['folder']}/{r['filename']}", key=r["key"], request=rid)
                    wake.set()
                    return {"deleting": True}
                await c.execute("DELETE FROM models WHERE id = $1", model_id)
                await audit(c, who, "model removed", f"{r['folder']}/{r['filename']}")
        if r["upload"] and s.models_dir:                     # its upload, kept for a Retry until now
            try:
                os.remove(os.path.join(s.models_dir, ".uploads", f"{r['upload']}.part"))
            except OSError:
                pass
        return {"deleted": True}

    # ---- the inbox (docs/design/18): files an operator copied into <model store>/inbox/<folder>/ --------------------
    @api.post("/api/admin/models/inbox")
    async def scan_inbox(body: InboxScan | None = None, x_comfyfarm_user: str | None = Header(default=None)):
        """Ask the builder to take in what the inbox holds; the models it took appear in the list, in the groups
        asked for here (none: no groups yet)."""
        who = ctx.admin(x_comfyfarm_user)
        need_builder()
        groups = sorted(set((body or InboxScan()).groups))
        async with db.conn() as c:
            await check_groups(c, groups)
            cur = await c.fetchval("SELECT value FROM settings WHERE key = $1", INBOX)
            if cur and (st := builder_status(s.builds_dir, cur.get("request", ""))) and st.get("state") in ("queued", "running"):
                raise HTTPException(409, "the builder is scanning the inbox already")
            rid = write_builder_request(s.builds_dir, {"kind": "model-inbox", "by": who})
            await c.execute("INSERT INTO settings(key, value, updated_by) VALUES ($1, $2, $3) ON CONFLICT (key) DO UPDATE "
                            "SET value = excluded.value, updated_by = excluded.updated_by, updated_at = now()",
                            INBOX, {"request": rid, "by": who, "registered": False, "groups": groups}, who)
            await audit(c, who, "model inbox scanned", None, request=rid)
        wake.set()
        return {"id": rid}

    async def inbox_state(c) -> dict | None:
        cur = await c.fetchval("SELECT value FROM settings WHERE key = $1", INBOX)
        if not cur or not s.builds_dir:
            return None
        st = builder_status(s.builds_dir, cur.get("request", "")) or {"state": "failed", "error": "the builder lost the scan"}
        return {"request": cur.get("request"), "state": st.get("state"), "error": st.get("error"), "groups": cur.get("groups") or [],
                "results": st.get("results") or [], "model": st.get("model"), "finished": st.get("finished")}

    async def register_inbox() -> bool:
        """A finished scan: the models it took in, into the list (ready, no groups) — once."""
        async with db.conn() as c:
            cur = await c.fetchval("SELECT value FROM settings WHERE key = $1", INBOX)
            if not cur or cur.get("registered") or not s.builds_dir:
                return False
            st = builder_status(s.builds_dir, cur.get("request", ""))
            if not st or st.get("state") not in ("done", "failed"):
                return False
            got = False
            async with c.transaction():
                groups = [g for g in cur.get("groups") or [] if isinstance(g, int)]
                folders = await model_folders(c)              # a folder an operator made in the inbox: the farm's own now
                new = sorted({x["folder"] for x in st.get("results") or [] if x.get("key") and not x.get("error")
                              and x.get("folder") not in folders and fetch.folder_ok(x.get("folder"))})
                if new:
                    await set_custom_folders(c, [f for f in folders if f not in fetch.MODEL_FOLDERS] + new, cur.get("by") or "?")
                for x in st.get("results") or []:
                    if x.get("key") and not x.get("error"):
                        got = True
                        mid = await c.fetchval(
                            "INSERT INTO models(folder, filename, source, size, sha256, key, state, bytes, created_by) "
                            "VALUES ($1,$2,'inbox',$3,$4,$5,'ready',$3,$6) ON CONFLICT (folder, filename) "
                            "WHERE state <> 'deleted' DO NOTHING RETURNING id", x["folder"], x["filename"], x.get("size"), x.get("sha256"), x["key"], cur.get("by"))
                        if mid is not None and groups:        # the groups asked for at the scan (a group gone since: none)
                            await c.execute("INSERT INTO group_models(group_id, model_id) SELECT g.id, $1 FROM worker_groups g "
                                            "WHERE g.id = ANY($2) ON CONFLICT DO NOTHING", mid, groups)
                await c.execute("UPDATE settings SET value = value || jsonb_build_object('registered', true) WHERE key = $1",
                                INBOX)
            return got

    # ---- the hosts' (fetch.py get-model) ------------------------------------------------------------------------
    def signed_entry(key: str) -> tuple[bytes, bytes]:
        if not s.models_dir:
            raise HTTPException(404, "this router has no model store (COMFYFARM_MODELS_DIR)")
        if not fetch.KEY.match(key):
            raise HTTPException(404, "no such model")
        lst = os.path.join(s.models_dir, "signed", "model", key + ".json")
        try:
            with open(lst, "rb") as f, open(lst + ".sig", "rb") as g:
                return f.read(), g.read()
        except OSError:
            raise HTTPException(404, f"model {key} is not in the store, or not signed yet (docker compose logs builder)")

    @api.get("/api/worker_models/{key}/signed")
    async def model_signed(key: str, request: Request):
        await worker_host(db, request)
        entry, sig = signed_entry(key)
        return {"entry": entry.decode(), "sig": sig.decode()}

    @api.get("/api/worker_models/{key}/file")
    async def model_file(key: str, request: Request):
        """The file as the store holds it, in ranges (an interrupted copy goes on); the host checks its SHA-256."""
        await worker_host(db, request)
        entry, _ = signed_entry(key)
        try:
            doc = fetch.model_doc(entry, key)
        except (ValueError, fetch.Refused):
            raise HTTPException(404, f"model {key}: its entry is not one")
        path = os.path.join(s.models_dir, doc["folder"], doc["filename"])
        if not os.path.isfile(path) or os.path.realpath(path) != os.path.join(os.path.realpath(s.models_dir), doc["folder"],
                                                                                  doc["filename"]):
            raise HTTPException(404, f"{doc['folder']}/{doc['filename']} is not in the store")
        return FileResponse(path, media_type="application/octet-stream")

    # ---- the background: builder states, copies on hosts ---------------------------------------------------------
    async def follow_builder() -> bool:
        """Queued and downloading models: what the builder wrote about their requests. True when one became ready.
        Deleting ones: deleted once the builder took them out of the store (hosts drop their copies: follow_copies)."""
        if not s.builds_dir:
            return False
        ready = False
        async with db.conn() as c:
            rows = await c.fetch("SELECT id, request, state FROM models WHERE state IN ('queued', 'downloading', 'deleting')")
        for r in rows:
            st = builder_status(s.builds_dir, r["request"]) if r["request"] else None
            async with db.conn() as c:
                if r["state"] == "deleting":
                    if st and st.get("state") == "done":
                        await c.execute("UPDATE models SET state = 'deleted', deleted_at = now(), error = NULL "
                                        "WHERE id = $1 AND request = $2", r["id"], r["request"])
                        ready = True                   # the hosts are told now
                    elif st is None or st.get("state") == "failed":
                        await c.execute("UPDATE models SET error = $2 WHERE id = $1 AND request = $3", r["id"],
                                        str((st or {}).get("error") or "the builder has no record of it — Delete again")[:2000],
                                        r["request"])
                    continue
                if st is None:
                    await c.execute("UPDATE models SET state = 'failed', error = $2 WHERE id = $1 AND state = $3", r["id"],
                                    "the builder has no record of this download (its status was deleted?) — Retry", r["state"])
                elif st.get("state") == "running":
                    await c.execute("UPDATE models SET state = 'downloading', bytes = $2, size = coalesce($3, size) "
                                    "WHERE id = $1 AND request = $4", r["id"], int(st.get("bytes") or 0),
                                    st.get("total") if isinstance(st.get("total"), int) else None, r["request"])
                elif st.get("state") == "done":
                    ready = True
                    await c.execute("UPDATE models SET state = 'ready', key = $2, sha256 = $3, size = $4, revision = $5, "
                                    "bytes = $4, error = NULL WHERE id = $1 AND request = $6", r["id"], st.get("key"),
                                    st.get("sha256"), st.get("size"), st.get("revision"), r["request"])
                elif st.get("state") == "failed":
                    await c.execute("UPDATE models SET state = 'failed', error = $2 WHERE id = $1 AND request = $3", r["id"],
                                    str(st.get("error") or "failed")[:2000], r["request"])
        return ready

    async def copies_of(w, names: list[str], unwanted: list[str] = ()) -> dict:
        """One host: how its copies stand; ask for the missing ones, and failed ones again now and then; drop the ones
        its group no longer uses or the farm deleted (`unwanted`: every other model the farm ever had)."""
        backend = be.make(w["restart_backend"], w["restart_config"], keys_dir=s.keys_dir)
        out: dict = {"wanted": len(names), "at": time.time()}
        if not names and not unwanted:
            return out | {"present": 0}
        try:
            got = await backend.fetch(sorted(set(names) | set(unwanted)), state=True)
        except (be.BackendError, OSError) as e:
            return out | {"error": str(e)[-300:]}
        if got is None:
            return out | {"error": "its agent keeps no copies (older worker tools, or another restart backend) — "
                                   "set the worker up again to have them"}
        st = {n: v for n, v in got.items() if n in names}
        stale = [n for n in unwanted if (got.get(n) or {}).get("state") == "present"]
        if stale:
            try:
                if await backend.forget(stale) is None:
                    out["error"] = (f"{len(stale)} copy(ies) of models it no longer uses stay — its worker tools predate "
                                    "removing them: set the worker up again")
                else:
                    out["removing"] = len(stale)
            except (be.BackendError, OSError) as e:
                out["error"] = str(e)[-300:]
        now = time.monotonic()
        ask = [n for n, v in st.items() if v.get("state") == "missing"
               or (v.get("state") == "failed" and now - asked.get((w["id"], n), 0) > RETRY_FAILED)]
        if ask:
            try:
                await backend.fetch(ask)
                asked.update({(w["id"], n): now for n in ask})
            except (be.BackendError, OSError) as e:
                out["error"] = str(e)[-300:]
        present = sum(v.get("state") == "present" for v in st.values())
        copying = {n: v for n, v in st.items() if v.get("state") in ("queued", "running")}
        failed = {n: v.get("error") or "?" for n, v in st.items() if v.get("state") == "failed"}
        return out | {"present": present, "copying": len(copying) + len(ask),
                      "bytes": sum(v.get("bytes") or 0 for v in copying.values()),
                      "total": sum(v.get("total") or 0 for v in copying.values()),
                      "failed": failed, "off": any(v.get("state") == "off" for v in st.values())}

    async def follow_copies() -> None:
        async with db.conn() as c:
            ws = await c.fetch(
                "SELECT w.id, w.group_id, i.restart_backend, i.restart_config FROM workers w "
                "JOIN instances i ON i.worker_id = w.id WHERE w.kind = 'gpu' AND w.deleted_at IS NULL AND w.enabled "
                "AND w.group_id IS NOT NULL AND coalesce(i.paths->>'model_store', '') <> ''")
            want = {}
            for r in await c.fetch("SELECT gm.group_id, m.key FROM group_models gm JOIN models m ON m.id = gm.model_id "
                                   "WHERE m.state = 'ready' AND m.key IS NOT NULL"):
                want.setdefault(r["group_id"], []).append(f"model/{r['key']}")
            known = [f"model/{r['key']}" for r in await c.fetch("SELECT DISTINCT key FROM models WHERE key IS NOT NULL")]
        for gone in set(copies) - {w["id"] for w in ws}:
            copies.pop(gone, None)
        before = {wid: x.get("present") for wid, x in copies.items()}
        limit = asyncio.Semaphore(8)

        async def one(w):
            async with limit:
                mine = sorted(want.get(w["group_id"], []))
                copies[w["id"]] = await copies_of(w, mine, [k for k in known if k not in set(mine)])
        await asyncio.gather(*(one(w) for w in ws))
        # a copy arrived or went: the editor's node definitions (its model lists) come from the group's snapshot, read
        # again from a worker that has the most copies — else the change would show only after a rollout
        grown = {w["group_id"] for w in ws if before.get(w["id"]) is not None
                 and (copies[w["id"]].get("present") or 0) != before[w["id"]]}
        refresh = getattr(router, "refresh_snapshot", None)
        for gid in sorted(grown) if refresh else []:
            mine = [w["id"] for w in ws if w["group_id"] == gid]
            most = max(copies[i].get("present") or 0 for i in mine)
            try:
                if await refresh(gid, prefer=[i for i in mine if (copies[i].get("present") or 0) == most]) is None:
                    log.warning("models: node definitions of group %s not read (no worker answered) — they are read "
                                "again when the editor next asks and one does", gid)
            except Exception as e:                  # noqa: BLE001 — the next copy tries again; Refresh nodes does too
                log.warning("models: node definitions of group %s not refreshed: %s", gid, type(e).__name__)

    async def model_loop():
        busy, last_copies = BusyLog(log, "models"), 0.0
        while True:
            try:
                copying = any(x.get("copying") for x in copies.values())      # followed closely while copies come
                if (await follow_builder() or await register_inbox() or wake.is_set()
                        or time.monotonic() - last_copies >= (TICK if copying else COPIES_EVERY)):
                    wake.clear()
                    last_copies = time.monotonic()
                    await follow_copies()
            except asyncio.CancelledError:
                raise
            except Busy as e:
                busy(e)
            except Exception as e:                  # noqa: BLE001 — logged, tried again next round
                log.warning("models: %s: %s", type(e).__name__, e)
            try:
                await asyncio.wait_for(wake.wait(), TICK)
            except asyncio.TimeoutError:
                pass

    router.background.append(model_loop)
    router.models_step = (follow_builder, follow_copies, copies)        # for tests
    router.models_register_inbox = register_inbox
    return api
