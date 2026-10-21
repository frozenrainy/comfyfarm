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


def check_model(m: ModelIn, formats: list[str] | None) -> dict:
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
    if m.folder not in fetch.MODEL_FOLDERS:
        raise ValueError(f"folder: one of {', '.join(fetch.MODEL_FOLDERS)}")
    filename = (m.filename or "").strip() or path.rsplit("/", 1)[-1]
    if not fetch.MODEL_FILE.match(filename):
        raise ValueError(f"file name: letters, digits, spaces and . _ + ( ) - — not {filename!r}")
    if formats is not None and not (filename.lower().endswith(tuple(formats)) and path.lower().endswith(tuple(formats))):
        raise ValueError(f"{filename}: only {', '.join(formats)} — other formats can run code when ComfyUI loads them "
                         "(docs/guide/models.md)")
    return {"repo": repo, "path": path, "ref": ref, "folder": m.folder, "filename": filename,
            "note": (m.note or "").strip() or None}


def row_out(r, groups: list[int]) -> dict:
    return {k: r[k] for k in ("id", "folder", "filename", "repo", "path", "ref", "revision", "size", "sha256", "key",
                              "state", "bytes", "error", "note", "created_by")} | {
        "created_at": r["created_at"].isoformat(), "groups": groups}


def make_router(ctx: Ctx) -> APIRouter:
    api = APIRouter()
    router, db, s = ctx.router, ctx.router.db, ctx.settings
    copies: dict[int, dict] = {}            # worker id → what its host said last (the dashboard's per-worker status)
    asked: dict[tuple[int, str], float] = {}  # (worker id, key) → when its copy was last asked for
    wake = asyncio.Event()                  # a model added, ready or given to a group: ask the hosts now

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
            rows = await c.fetch("SELECT * FROM models ORDER BY folder, filename")
            links = await c.fetch("SELECT model_id, group_id FROM group_models")
            workers = await c.fetch("SELECT w.id, w.name, w.group_id, w.state, "
                                    "coalesce(i.paths->>'model_store', '') <> '' AS copies "
                                    "FROM workers w LEFT JOIN instances i ON i.worker_id = w.id "
                                    "WHERE w.kind = 'gpu' AND w.deleted_at IS NULL ORDER BY w.name")
        by_model: dict[int, list[int]] = {}
        for x in links:
            by_model.setdefault(x["model_id"], []).append(x["group_id"])
        return {"builder": {"configured": bool(s.builds_dir), "up": bool(p and p["up"]), "models": bool(p and p.get("models")),
                            "formats": (p or {}).get("model_formats"), "hf_token": bool(p and p.get("hf_token")),
                            "store": bool(s.models_dir), "folders": list(fetch.MODEL_FOLDERS)},
                "models": [row_out(r, sorted(by_model.get(r["id"], []))) for r in rows],
                "workers": [{"id": w["id"], "name": w["name"], "group_id": w["group_id"], "state": w["state"],
                             "copies": bool(w["copies"]), **copies.get(w["id"], {})} for w in workers]}

    @api.post("/api/admin/models")
    async def add_models(body: ModelsIn, x_comfyfarm_user: str | None = Header(default=None)):
        """One model or a pasted list: all of it is checked first, then each goes to the builder as its own request."""
        who = ctx.admin(x_comfyfarm_user)
        p = need_builder()
        if not body.models or len(body.models) > MAX_BATCH:
            raise HTTPException(400, f"1 to {MAX_BATCH} models at a time")
        items, seen = [], set()
        for i, m in enumerate(body.models, 1):
            try:
                it = check_model(m, p.get("model_formats"))
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
        """Which groups use it. Taken out of a group, its copies stay on that group's workers (removing them comes
        with removing models, docs/design/17 phase 2); added to one, its workers get a copy in the background."""
        who = ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            async with c.transaction():
                r = await c.fetchrow("SELECT folder, filename FROM models WHERE id = $1 FOR UPDATE", model_id)
                if r is None:
                    raise HTTPException(404, "no such model")
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
                rid = write_builder_request(s.builds_dir, {"kind": "model", **{k: r[k] for k in (
                    "repo", "path", "ref", "folder", "filename")}, "by": who})
                await c.execute("UPDATE models SET state = 'queued', error = NULL, request = $2 WHERE id = $1", model_id, rid)
                await audit(c, who, "model retried", f"{r['folder']}/{r['filename']}", request=rid)
        wake.set()
        return {"id": rid}

    @api.delete("/api/admin/models/{model_id}")
    async def delete_model(model_id: int, x_comfyfarm_user: str | None = Header(default=None)):
        """Only one that never arrived (a typo, a gated model): nothing of it is in the store or on a worker. One in
        the store stays until removing models comes (docs/design/17 phase 2)."""
        who = ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            async with c.transaction():
                r = await c.fetchrow("SELECT folder, filename, state FROM models WHERE id = $1 FOR UPDATE", model_id)
                if r is None:
                    raise HTTPException(404, "no such model")
                if r["state"] != "failed":
                    raise HTTPException(409, "only a model that failed can be removed for now — one in the store stays "
                                             "(take it out of its groups to stop new copies)")
                await c.execute("DELETE FROM models WHERE id = $1", model_id)
                await audit(c, who, "model removed", f"{r['folder']}/{r['filename']}")
        return {"deleted": True}

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
        """Queued and downloading models: what the builder wrote about their requests. True when one became ready."""
        if not s.builds_dir:
            return False
        ready = False
        async with db.conn() as c:
            rows = await c.fetch("SELECT id, request, state FROM models WHERE state IN ('queued', 'downloading')")
        for r in rows:
            st = builder_status(s.builds_dir, r["request"]) if r["request"] else None
            async with db.conn() as c:
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

    async def copies_of(w, names: list[str]) -> dict:
        """One host: how its copies stand; ask for the missing ones, and failed ones again now and then."""
        backend = be.make(w["restart_backend"], w["restart_config"], keys_dir=s.keys_dir)
        out: dict = {"wanted": len(names), "at": time.time()}
        if not names:
            return out | {"present": 0}
        try:
            st = await backend.fetch(names, state=True)
        except (be.BackendError, OSError) as e:
            return out | {"error": str(e)[-300:]}
        if st is None:
            return out | {"error": "its agent keeps no copies (older worker tools, or another restart backend) — "
                                   "set the worker up again to have them"}
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
        for gone in set(copies) - {w["id"] for w in ws}:
            copies.pop(gone, None)
        before = {wid: x.get("present") for wid, x in copies.items()}
        limit = asyncio.Semaphore(8)

        async def one(w):
            async with limit:
                copies[w["id"]] = await copies_of(w, sorted(want.get(w["group_id"], [])))
        await asyncio.gather(*(one(w) for w in ws))
        # a copy arrived: the editor's node definitions (its model lists) come from the group's snapshot, read again
        # from a worker that has the most copies — else the new model would show only after a rollout
        grown = {w["group_id"] for w in ws if before.get(w["id"]) is not None
                 and (copies[w["id"]].get("present") or 0) > before[w["id"]]}
        refresh = getattr(router, "refresh_snapshot", None)
        for gid in sorted(grown) if refresh else []:
            mine = [w["id"] for w in ws if w["group_id"] == gid]
            most = max(copies[i].get("present") or 0 for i in mine)
            try:
                await refresh(gid, prefer=[i for i in mine if (copies[i].get("present") or 0) == most])
            except Exception as e:                  # noqa: BLE001 — the next copy tries again; Refresh nodes does too
                log.warning("models: node definitions of group %s not refreshed: %s", gid, type(e).__name__)

    async def model_loop():
        busy, last_copies = BusyLog(log, "models"), 0.0
        while True:
            try:
                copying = any(x.get("copying") for x in copies.values())      # followed closely while copies come
                if (await follow_builder() or wake.is_set()
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
    return api
