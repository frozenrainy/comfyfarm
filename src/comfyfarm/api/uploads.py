"""Uploads (docs/design/18): models and node pack archives from the operator's own files, in chunks that an
interrupted upload goes on from. The router keeps them in two places only — <model store>/.uploads (a model, until the
builder has copied it into place, hashed and signed) and <builds>/archives (node pack archives, by SHA-256, for the
builder to unpack when it builds) — and decides nothing about their contents: the builder checks both."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
import uuid

import asyncpg
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from .. import fetch, publish
from ..db import Busy, BusyLog
from ..rollout.engine import audit
from .builds import read_policy, write_builder_request
from .context import Ctx

log = logging.getLogger(__name__)
CHUNK = 16 << 20                    # what the dashboard sends at a time
MAX_PUT = 64 << 20                  # what one request may carry
MAX_PACK = 2 << 30
STALE = 24 * 3600.0                 # an upload not finished in a day is dropped
ARCHIVE = re.compile(r"^[^/\\\x00]{1,200}\.(zip|tar\.gz|tgz|tar)$", re.I)
SNIFF = ((b"PK\x03\x04", "zip"), (b"\x1f\x8b", "tar.gz"))


class UploadIn(BaseModel):
    kind: str                           # model | nodepack
    filename: str
    size: int
    meta: dict = {}


def archive_kind(path: str) -> str | None:
    """What an archive is by its first bytes, not its name: zip, tar.gz or tar."""
    with open(path, "rb") as f:
        head = f.read(262)
    for magic, kind in SNIFF:
        if head.startswith(magic):
            return kind
    return "tar" if head[257:262] == b"ustar" else None


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def doc(r) -> dict:
    return {"id": str(r["id"]), "kind": r["kind"], "filename": r["filename"], "size": r["size"], "received": r["received"],
            "state": r["state"], "meta": r["meta"], "error": r["error"], "created_by": r["created_by"],
            "updated_at": r["updated_at"].isoformat()}


def make_router(ctx: Ctx) -> APIRouter:
    api = APIRouter()
    router, db, s = ctx.router, ctx.router.db, ctx.settings
    locks: dict[str, asyncio.Lock] = {}

    def model_uploads() -> str:
        d = os.path.join(s.models_dir or "", ".uploads")
        if not s.models_dir or not os.path.isdir(d) or not os.access(d, os.W_OK):
            raise HTTPException(400, f"the router cannot write {d} — the deployment mounts it for uploads (compose: "
                                     "COMFYFARM_MODELS_MOUNT, docs/guide/models.md)")
        return d

    def pack_archives() -> str:
        if not s.builds_dir:
            raise HTTPException(400, "no control-plane builder here (the compose deployment's builder service)")
        d = os.path.join(s.builds_dir, "archives")
        os.makedirs(os.path.join(d, ".incoming"), exist_ok=True)
        return d

    def part_of(kind: str, uid: str) -> str:
        return os.path.join(model_uploads(), uid + ".part") if kind == "model" else \
            os.path.join(pack_archives(), ".incoming", uid + ".part")

    def need_policy(what: str) -> dict:
        p = read_policy(s.builds_dir)
        if not p or not p["up"]:
            raise HTTPException(503, "the builder is not running (docker compose ps builder; its log says why)")
        if what == "model" and not p.get("models"):
            raise HTTPException(400, "the builder has no model store (COMFYFARM_MODELS, docs/guide/models.md)")
        if what == "nodepack" and not p.get("node_archives"):
            raise HTTPException(400, "the builder takes no node pack archives (its COMFYFARM_BUILD_NODE_ARCHIVES is off — "
                                     "docs/design/18)")
        return p

    async def check_model(c, body: UploadIn, p: dict) -> dict:
        m = body.meta
        folder, groups = m.get("folder"), m.get("groups") or []
        if folder not in fetch.MODEL_FOLDERS:
            raise HTTPException(400, f"folder: one of {', '.join(fetch.MODEL_FOLDERS)}")
        if not fetch.MODEL_FILE.match(body.filename):
            raise HTTPException(400, f"file name: letters, digits, spaces and . _ + ( ) - — not {body.filename!r}")
        formats = p.get("model_formats")
        if formats is not None and not body.filename.lower().endswith(tuple(formats)):
            raise HTTPException(400, f"{body.filename}: only {', '.join(formats)} — other formats can run code when ComfyUI "
                                     "loads them (docs/guide/models.md)")
        if not isinstance(groups, list) or not all(isinstance(g, int) for g in groups):
            raise HTTPException(400, "groups: a list of group ids")
        if groups and len({r["id"] for r in await c.fetch("SELECT id FROM worker_groups WHERE id = ANY($1)", groups)}) != len(set(groups)):
            raise HTTPException(400, "no such group")
        if await c.fetchval("SELECT 1 FROM models WHERE folder = $1 AND filename = $2", folder, body.filename) or \
                await c.fetchval("SELECT 1 FROM uploads WHERE kind = 'model' AND state = 'uploading' AND meta->>'folder' = $1 "
                                 "AND filename = $2", folder, body.filename):
            raise HTTPException(409, f"{folder}/{body.filename} is in the list already")
        return {"folder": folder, "groups": sorted(set(groups)), "note": (str(m.get("note") or "").strip() or None)}

    async def check_pack(c, body: UploadIn) -> dict:
        m = body.meta
        if not ARCHIVE.match(body.filename):
            raise HTTPException(400, "an archive: .zip, .tar.gz, .tgz or .tar")
        if body.size > MAX_PACK:
            raise HTTPException(400, f"at most {MAX_PACK >> 30} GiB — a node pack that large carries models: put them in "
                                     "the model store")
        if m.get("pack_id") is not None:                          # a new version of an archive pack
            r = await c.fetchrow("SELECT name, archive FROM node_packs WHERE id = $1", m["pack_id"])
            if r is None or r["archive"] is None:
                raise HTTPException(404, "no such archive node pack")
            return {"pack_id": int(m["pack_id"]), "name": r["name"]}
        name = str(m.get("name") or "").strip()
        if not publish.NAME.match(name) or name in ("comfyfarm_worker", "comfyfarm_ui"):
            raise HTTPException(400, "name: letters, digits and . _ - (the pack's directory under custom_nodes)")
        if await c.fetchval("SELECT 1 FROM node_packs WHERE name = $1", name) or \
                await c.fetchval("SELECT 1 FROM uploads WHERE kind = 'nodepack' AND state = 'uploading' AND meta->>'name' = $1", name):
            raise HTTPException(409, f"a node pack named {name!r} exists")
        rf = str(m.get("requirements_file") or "").strip() or None
        if rf and not all(publish.REQ_FILE.match(f.strip()) for f in rf.split(",")):
            raise HTTPException(400, "requirements_file: .txt files inside the pack, comma-separated")
        cons = [x.strip() for x in (m.get("constraints") or []) if isinstance(x, str) and x.strip()]
        if bad := [x for x in cons if "\n" in x or not publish.SPEC.match(x)]:
            raise HTTPException(400, f"constraints: version specifiers like 'kornia<0.8', not {bad[0]!r}")
        return {"name": name, "ui": bool(m.get("ui", True)), "requirements_file": rf, "constraints": cons,
                "note": (str(m.get("note") or "").strip() or None)}

    @api.post("/api/admin/uploads")
    async def start(body: UploadIn, x_comfyfarm_user: str | None = Header(default=None)):
        who = ctx.admin(x_comfyfarm_user)
        if body.kind not in ("model", "nodepack"):
            raise HTTPException(400, "kind: model or nodepack")
        if body.size <= 0:
            raise HTTPException(400, "an empty file")
        p = need_policy(body.kind)
        uid = str(uuid.uuid4())
        async with db.conn() as c:
            meta = await check_model(c, body, p) if body.kind == "model" else await check_pack(c, body)
            path = part_of(body.kind, uid)
            with open(path, "wb"):
                pass
            os.chmod(path, 0o644)                         # the builder reads it (another account)
            r = await c.fetchrow("INSERT INTO uploads(id, kind, filename, size, meta, created_by) VALUES ($1,$2,$3,$4,$5,$6) "
                                 "RETURNING *", uuid.UUID(uid), body.kind, body.filename, body.size, meta, who)
        return {**doc(r), "chunk": CHUNK}

    @api.get("/api/admin/uploads")
    async def unfinished(x_comfyfarm_user: str | None = Header(default=None)):
        """Uploads to go on with (pick the same file again) and the last finished ones."""
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            rows = await c.fetch("(SELECT * FROM uploads WHERE state = 'uploading' ORDER BY created_at) UNION ALL "
                                 "(SELECT * FROM uploads WHERE state <> 'uploading' ORDER BY updated_at DESC LIMIT 10)")
        return [doc(r) for r in rows]

    async def one(c, uid: str):
        try:
            u = uuid.UUID(uid)
        except ValueError:
            raise HTTPException(404, "no such upload")
        r = await c.fetchrow("SELECT * FROM uploads WHERE id = $1", u)
        if r is None:
            raise HTTPException(404, "no such upload")
        return r

    @api.put("/api/admin/uploads/{uid}")
    async def put(uid: str, request: Request, offset: int, x_comfyfarm_user: str | None = Header(default=None)):
        """A chunk at `offset`, which must be what the router has; anything else is answered 409 with what it has —
        the dashboard goes on from there."""
        who = ctx.admin(x_comfyfarm_user)
        lock = locks.setdefault(uid, asyncio.Lock())
        async with lock:
            async with db.conn() as c:
                r = await one(c, uid)
            if r["state"] != "uploading":
                raise HTTPException(409, {"message": f"the upload is {r['state']}", "received": r["received"], "state": r["state"]})
            if offset != r["received"]:
                raise HTTPException(409, {"message": f"the router has {r['received']} bytes", "received": r["received"],
                                          "state": r["state"]})
            path = part_of(r["kind"], uid)
            if not os.path.isfile(path) or os.path.getsize(path) < r["received"]:
                raise HTTPException(409, {"message": "the part on disk is gone — start the upload again", "received": 0,
                                          "state": "failed"})
            buf = bytearray()
            async for piece in request.stream():
                buf += piece
                if len(buf) > MAX_PUT or offset + len(buf) > r["size"]:
                    raise HTTPException(413, f"more than the file's {r['size']} bytes, or more than {MAX_PUT >> 20} MiB at once")

            def write():
                with open(path, "r+b") as f:
                    f.seek(offset)
                    f.write(buf)
                    f.truncate()
            await asyncio.to_thread(write)
            received = offset + len(buf)
            async with db.conn() as c:
                await c.execute("UPDATE uploads SET received = $2, updated_at = now() WHERE id = $1", r["id"], received)
            result = None
            if received == r["size"]:
                result = await finish(r, path, who)
        if received == r["size"]:
            locks.pop(uid, None)
        return {"received": received, "size": r["size"], "done": received == r["size"], "result": result}

    async def finish(r, path: str, who: str) -> dict:
        """The whole file is here: a model goes to the builder (copied into place, hashed, signed); an archive is
        filed under its SHA-256 for the builder to unpack, and the node pack names it."""
        meta = dict(r["meta"])
        try:
            if r["kind"] == "model":
                out = await finish_model(r, meta, who)
            else:
                out = await finish_pack(r, path, meta, who)
        except HTTPException as e:
            async with db.conn() as c:
                await c.execute("UPDATE uploads SET state = 'failed', error = $2, updated_at = now() WHERE id = $1",
                                r["id"], str(e.detail)[:500])
            raise
        async with db.conn() as c:
            await c.execute("UPDATE uploads SET state = 'done', meta = meta || $2::jsonb, updated_at = now() WHERE id = $1",
                            r["id"], out)
        return out

    async def finish_model(r, meta: dict, who: str) -> dict:
        async with db.conn() as c:
            async with c.transaction():
                try:
                    mid = await c.fetchval("INSERT INTO models(folder, filename, source, upload, size, note, created_by) "
                                           "VALUES ($1,$2,'upload',$3,$4,$5,$6) RETURNING id", meta["folder"], r["filename"],
                                           r["id"], r["size"], meta.get("note"), who)
                except asyncpg.UniqueViolationError:
                    raise HTTPException(409, f"{meta['folder']}/{r['filename']} is in the list already")
                for g in meta.get("groups") or []:
                    await c.execute("INSERT INTO group_models(group_id, model_id) VALUES ($1, $2) ON CONFLICT DO NOTHING", g, mid)
                rid = write_builder_request(s.builds_dir, {"kind": "model-file", "upload": str(r["id"]), "folder": meta["folder"],
                                                           "filename": r["filename"], "size": r["size"], "by": who})
                await c.execute("UPDATE models SET request = $2 WHERE id = $1", mid, rid)
                await audit(c, who, "model uploaded", f"{meta['folder']}/{r['filename']}", size=r["size"], request=rid,
                            groups=meta.get("groups") or [])
        wake = getattr(router, "models_wake", None)
        if wake:
            wake.set()
        return {"model_id": mid}

    async def finish_pack(r, path: str, meta: dict, who: str) -> dict:
        kind = await asyncio.to_thread(archive_kind, path)
        if kind is None:
            raise HTTPException(400, f"{r['filename']} is not a zip or tar archive")
        sha = await asyncio.to_thread(sha256_of, path)
        dest = os.path.join(pack_archives(), f"{sha}.{kind}")
        os.replace(path, dest)
        os.chmod(dest, 0o644)
        async with db.conn() as c:
            async with c.transaction():
                if meta.get("pack_id") is not None:
                    await c.execute("UPDATE node_packs SET archive = $2, archive_name = $3, updated_at = now() WHERE id = $1",
                                    meta["pack_id"], sha, r["filename"])
                    pid = meta["pack_id"]
                    await audit(c, who, "node pack changed", meta["name"], archive=sha, file=r["filename"])
                else:
                    try:
                        pid = await c.fetchval(
                            "INSERT INTO node_packs(name, archive, archive_name, ui, requirements_file, constraints, note, added_by) "
                            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id", meta["name"], sha, r["filename"], meta["ui"],
                            meta.get("requirements_file"), meta.get("constraints") or [], meta.get("note"), who)
                    except asyncpg.UniqueViolationError:
                        raise HTTPException(409, f"a node pack named {meta['name']!r} exists")
                    await audit(c, who, "node pack added", meta["name"], archive=sha, file=r["filename"])
        return {"pack_id": pid, "archive": sha}

    @api.delete("/api/admin/uploads/{uid}")
    async def cancel(uid: str, x_comfyfarm_user: str | None = Header(default=None)):
        ctx.admin(x_comfyfarm_user)
        async with db.conn() as c:
            r = await one(c, uid)
            if r["state"] != "uploading":
                raise HTTPException(409, f"the upload is {r['state']}")
            await c.execute("UPDATE uploads SET state = 'cancelled', updated_at = now() WHERE id = $1", r["id"])
        try:
            os.remove(part_of(r["kind"], uid))
        except (OSError, HTTPException):
            pass
        return {"cancelled": True}

    # ---- the background: unfinished uploads go after a day; a model's upload once the builder has it --------------
    async def tidy() -> None:
        async with db.conn() as c:
            stale = await c.fetch("UPDATE uploads SET state = 'cancelled', error = 'not finished within a day', updated_at = now() "
                                  "WHERE state = 'uploading' AND updated_at < now() - make_interval(secs => $1) "
                                  "RETURNING id, kind", STALE)
            taken = await c.fetch("SELECT u.id, u.kind FROM uploads u JOIN models m ON m.upload = u.id "
                                  "WHERE u.state = 'done' AND m.state = 'ready' AND NOT (u.meta ? 'removed')")
            for r in taken:
                await c.execute("UPDATE uploads SET meta = meta || '{\"removed\": true}'::jsonb WHERE id = $1", r["id"])
        for r in list(stale) + list(taken):
            try:
                os.remove(part_of(r["kind"], str(r["id"])))
            except (OSError, HTTPException):
                pass

    async def tidy_loop():
        busy = BusyLog(log, "uploads")
        while True:
            try:
                await tidy()
            except asyncio.CancelledError:
                raise
            except Busy as e:
                busy(e)
            except Exception as e:                  # noqa: BLE001 — logged, tried again
                log.warning("uploads: %s: %s", type(e).__name__, e)
            await asyncio.sleep(60)

    router.background.append(tidy_loop)
    router.uploads_tidy = tidy                                     # for tests
    return api
