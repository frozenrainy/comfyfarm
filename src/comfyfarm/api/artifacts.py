"""The store's artifacts for GPU hosts that keep copies (docs/design/16). No user identity: the caller is a host's fetch
service, and only the hosts of this farm's instances are answered. The router hands on what it reads — the builder's
signed list of files and the files themselves — and could not change them unnoticed: the host checks both against the
builder's key it pinned at setup."""
from __future__ import annotations

import asyncio
import os
import tarfile
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import fetch
from .context import Ctx

CHUNK = 1 << 20


def tar_stream(root: str):
    """A file object to read the artifact's tree from as an uncompressed tar, written by a thread: entries in path
    order, directories before what they hold, READY left out, hard links as plain files (the signed list has files)."""
    r, w = os.pipe()

    def produce():
        try:
            with os.fdopen(w, "wb") as out, tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                tar.add(root, arcname=".", recursive=False)
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames.sort()
                    for n in sorted(dirnames + filenames):
                        p = os.path.join(dirpath, n)
                        rel = os.path.relpath(p, root)
                        if rel == "READY":
                            continue
                        tar.inodes.clear()                 # never a link entry to a file sent before
                        tar.add(p, arcname="./" + rel, recursive=False)
        except (BrokenPipeError, OSError):
            pass                                           # the host went away; it asks again
    threading.Thread(target=produce, daemon=True, name="artifact-tar").start()
    return os.fdopen(r, "rb")


def make_router(ctx: Ctx) -> APIRouter:
    api = APIRouter()
    db, s = ctx.router.db, ctx.settings

    async def worker_host(request: Request) -> None:
        caller = request.client.host if request.client else ""
        async with db.conn() as c:
            hosts = {h for r in await c.fetch("SELECT hosts FROM instances") for h in (r["hosts"] or [])}
        if caller in hosts:
            return
        from .builds import resolve_hosts
        if caller not in await resolve_hosts(sorted(hosts)):
            raise HTTPException(403, "not a host of this farm's instances")

    def published(kind: str, name: str) -> str:
        if not s.artifacts_dir:
            raise HTTPException(404, "this router has no artifact store")
        if kind not in fetch.KINDS or not fetch.NAME.match(name):
            raise HTTPException(404, "no such artifact")
        d = os.path.join(s.artifacts_dir, kind, name)
        if not os.path.isfile(os.path.join(d, "READY")):
            raise HTTPException(404, f"{kind}/{name} is not published here")
        return d

    @api.get("/api/worker_artifacts/{kind}/{name}/signed")
    async def signed(kind: str, name: str, request: Request):
        await worker_host(request)
        published(kind, name)
        lst = os.path.join(s.artifacts_dir, "signed", kind, name + ".json")
        try:
            with open(lst) as f, open(lst + ".sig") as g:
                return {"list": f.read(), "sig": g.read()}
        except OSError:
            raise HTTPException(404, f"{kind}/{name} is not signed yet — the builder signs what it published when it "
                                     "starts and after each build (docker compose logs builder)")

    @api.get("/api/worker_artifacts/{kind}/{name}/tar")
    async def tar(kind: str, name: str, request: Request):
        await worker_host(request)
        src = tar_stream(published(kind, name))
        loop = asyncio.get_running_loop()

        async def body():
            try:
                while chunk := await loop.run_in_executor(None, src.read, CHUNK):
                    yield chunk
            finally:
                src.close()
        return StreamingResponse(body(), media_type="application/x-tar")

    return api
