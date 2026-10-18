"""`comfyfarm doctor` — the questions an operator asks when something is off, answered in one run and in plain
words: can the ledger be reached and written, is disk running out, are the workers reachable and locked down, do the
workers of a group agree on nodes and models, is shared storage really shared, is a rollout stuck.

Each check yields (status, title, detail) with status OK / WARN / FAIL. Exit code 1 when anything FAILs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

import asyncpg
import httpx

from .adapter.comfy import ComfyAdapter
from .core import consistency
from .core.settings import Settings

OK, WARN, FAIL = "OK", "WARN", "FAIL"
DISK_WARN_FREE = 0.10          # less than 10 % free → WARN
TINY_PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478"
                         "9c6360000002000154a24f5d0000000049454e44ae426082")


def backup_tooling(dump: str, server_major: int) -> tuple[str, str, str]:
    """pg_dump refuses a newer server; a dump from a newer pg_dump carries settings an older server rejects on
    restore (17 → 16: `SET transaction_timeout`). Same major version is the only pair that both backs up and restores."""
    try:
        m = re.search(r"(\d+)(?:\.\d+)?", subprocess.run([dump, "--version"], capture_output=True, text=True, timeout=10).stdout)
    except (OSError, subprocess.SubprocessError) as e:
        return WARN, "backup tooling", f"{dump} does not run: {type(e).__name__}"
    client = int(m.group(1)) if m else 0
    if client < server_major:
        return FAIL, "backup tooling", f"pg_dump {client} cannot dump PostgreSQL {server_major} — install the {server_major} client tools"
    if client > server_major:
        return WARN, "backup tooling", (f"pg_dump {client} with PostgreSQL {server_major}: its dumps do not restore into "
                                        f"this server — use the {server_major} client tools (--pg-bin) or upgrade the server")
    return OK, "backup tooling", f"pg_dump {client} matches the server ({dump})"


async def run(s: Settings, ui_url: str | None = None, pg_bin: str | None = None) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    # ---- ledger -------------------------------------------------------------------------------------------------
    try:
        conn = await asyncpg.connect(s.dsn, timeout=10)
        await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    except Exception as e:
        out.append((FAIL, "ledger", f"cannot connect: {type(e).__name__}: {e}"))
        return out
    try:
        ver = await conn.fetchval("SHOW server_version")
        server_major = int(await conn.fetchval("SHOW server_version_num")) // 10000
        ro = await conn.fetchval("SELECT pg_is_in_recovery()")
        try:
            migs = await conn.fetchval("SELECT count(*) FROM schema_migrations")
        except asyncpg.UndefinedTableError:
            out.append((FAIL, "ledger", f"Postgres {ver}, but the ledger is empty — start the router once "
                                        "(`comfyfarm serve`), it creates the tables"))
            return out
        out.append((FAIL if ro else OK, "ledger", f"Postgres {ver}, {migs} migrations applied"
                    + (", READ-ONLY (standby?) — the router cannot work" if ro else "")))
        size = await conn.fetchval("SELECT pg_size_pretty(pg_database_size(current_database()))")
        jobs = await conn.fetchrow("SELECT count(*) AS n, count(*) FILTER (WHERE status IN ('pending','paused')) AS waiting, "
                                   "count(*) FILTER (WHERE status IN ('dispatching','running','cancelling')) AS running FROM jobs")
        out.append((OK, "jobs", f"{jobs['n']} in the ledger ({jobs['waiting']} waiting, {jobs['running']} running), database {size}"))
        admins = await conn.fetchval("SELECT count(*) FROM users WHERE 'admin' = ANY(roles) AND disabled_at IS NULL")
        out.append((OK if admins else WARN, "accounts", f"{admins} active administrator(s)"
                    + ("" if admins else " — create one: comfyfarm add-user <name> --admin")))
        # ---- rollouts -------------------------------------------------------------------------------------------
        stuck = await conn.fetch("SELECT id, status, created_at FROM rollouts WHERE status IN ('running','aborting','interrupted')")
        for r in stuck:
            age = time.time() - r["created_at"].timestamp()
            out.append((WARN if r["status"] == "interrupted" else OK, f"rollout #{r['id']}",
                        f"{r['status']} for {age / 60:.0f} min" + (" — resume or abort it in System → Rollout" if r["status"] == "interrupted" else "")))
        # ---- workers --------------------------------------------------------------------------------------------
        instances = [dict(r) for r in await conn.fetch(
            "SELECT i.instance, i.restart_backend, i.restart_config FROM instances i JOIN workers w ON w.id = i.worker_id "
            "WHERE w.deleted_at IS NULL AND w.enabled AND i.restart_backend IN ('ssh', 'http-agent') ORDER BY i.instance")]
        workers = [dict(r) for r in await conn.fetch(
            "SELECT w.id, w.name, w.kind, w.base_url, w.group_id, w.enabled, w.maintenance, w.state, w.capabilities, g.name AS group_name "
            "FROM workers w LEFT JOIN worker_groups g ON g.id = w.group_id WHERE w.deleted_at IS NULL ORDER BY w.name")]
    finally:
        await conn.close()
    adapter = ComfyAdapter()
    try:
        if not workers:
            out.append((WARN, "workers", "none registered — add them in System → Workers"))
        for w in workers:
            if not w["enabled"]:
                out.append((OK, f"worker {w['name']}", "disabled"))
                continue
            ok, kind, detail = await adapter.probe(w["base_url"])
            if not ok:
                out.append((FAIL, f"worker {w['name']}", f"not answering ({kind}: {detail}) — {w['base_url']}"))
                continue
            info = await adapter.worker_info(w["base_url"])
            bits = [f"online{' (maintenance)' if w['maintenance'] else ''}",
                    "UI worker" if w["kind"] == "ui" else f"group {w['group_name'] or '— NONE: receives no jobs'}"]
            st = OK
            if w["group_id"] is None and w["kind"] == "gpu":
                st = WARN
            if info is None:
                bits.append("extension: no answer"); st = WARN
            elif not info.get("installed"):
                bits.append("extension NOT installed: open to everyone on the network"); st = WARN
            elif not info.get("secured"):
                bits.append("UNSECURED: set COMFYFARM_ALLOW on the worker"); st = WARN
            else:
                bits.append(f"secured ({', '.join(info.get('allow', []))})")
            caps = w["capabilities"] or {}
            if caps.get("backend"):
                bits.append(f"{caps['backend']} ComfyUI {caps.get('comfyui')}")
            out.append((st, f"worker {w['name']}", " · ".join(bits)))
        # ---- group consistency -----------------------------------------------------------------------------------
        rep = consistency.report(workers)
        for gid, g in rep.items():
            name = next((w["group_name"] for w in workers if w["group_id"] == gid), str(gid))
            if g["inconsistent"]:
                for wid, d in g["workers"].items():
                    if not d["consistent"]:
                        wname = next(w["name"] for w in workers if w["id"] == wid)
                        for line in consistency.warnings(d):
                            out.append((WARN, f"group {name}", f"{wname} {line}"))
            else:
                out.append((OK, f"group {name}", f"{len(g['workers'])} worker(s) agree on nodes and models"))
        # ---- shared storage ----------------------------------------------------------------------------------------
        if not ui_url:                      # the UI worker set up in the dashboard
            ui_url = next((w["base_url"] for w in workers if w["kind"] == "ui" and w["enabled"]), None)
        if ui_url:
            name = "comfyfarm-doctor.png"
            try:
                r = await adapter.http.post(f"{ui_url.rstrip('/')}/upload/image", timeout=20,
                                            files={"image": (name, TINY_PNG, "image/png")}, data={"overwrite": "true"})
                if r.status_code != 200:
                    out.append((FAIL, "shared storage", f"upload to the UI worker answered {r.status_code}"
                                + (" — if it runs with COMFYFARM_ALLOW, the control plane's address must be in it"
                                   if r.status_code == 403 else " — its log says why (e.g. an input directory or a file there it "
                                   "cannot write as its account)")))
                else:
                    for w in workers:
                        if not w["enabled"] or w["state"] != "online" or w["base_url"].rstrip("/") == ui_url.rstrip("/"):
                            continue
                        v = await adapter.http.get(f"{w['base_url']}/view", params={"filename": name, "type": "input"}, timeout=10)
                        out.append((OK if v.status_code == 200 else FAIL, "shared storage",
                                    f"{w['name']}: " + ("sees files uploaded through the UI worker" if v.status_code == 200 else
                                                        f"does NOT see the uploaded file ({v.status_code}) — input/output must be one shared filesystem (design 01)")))
            except httpx.HTTPError as e:
                out.append((FAIL, "shared storage", f"check failed: {type(e).__name__}"))
        else:
            out.append((WARN, "shared storage", "not checked — pass --ui <UI worker URL> to verify that GPU workers see uploads"))
    finally:
        await adapter.close()
    # ---- host checks through the agents (docs/design/14) -----------------------------------------------------------
    from .rollout import backends as be
    for inst in instances:
        label = f"host {inst['instance']}"
        try:
            rows = await be.make(inst["restart_backend"], inst["restart_config"], keys_dir=s.keys_dir).check(inst["instance"])
        except be.BackendError as e:
            out.append((WARN, label, f"host check not run: {e}"))
            continue
        fails = [r for r in rows or [] if r.get("level") == FAIL]
        warns = [r for r in rows or [] if r.get("level") == WARN]
        if fails or warns:
            worst = (fails or warns)[0]
            out.append((FAIL if fails else WARN, label, f"{len(fails)} failed, {len(warns)} warning(s) — {worst['name']}: "
                        f"{worst['detail']}"))
        else:
            out.append((OK, label, f"{len(rows or [])} checks OK"))

    # ---- disk, artifacts -----------------------------------------------------------------------------------------
    for label, path in (("disk (router)", os.getcwd()), ("disk (artifacts)", s.artifacts_dir)):
        if not path:
            continue
        try:
            u = shutil.disk_usage(path)
        except OSError:
            out.append((WARN, label, f"{path}: not accessible"))
            continue
        frac = u.free / u.total if u.total else 0
        out.append((WARN if frac < DISK_WARN_FREE else OK, label, f"{u.free / 2**30:.1f} GB free of {u.total / 2**30:.0f} GB ({frac:.0%})"))
    if s.artifacts_dir:
        counts = {}
        for kind in ("core", "venv", "nodes", "frontend"):
            d = os.path.join(s.artifacts_dir, kind)
            counts[kind] = sum(1 for n in (os.listdir(d) if os.path.isdir(d) else []) if os.path.isfile(os.path.join(d, n, "READY")))
        out.append((OK, "artifacts", ", ".join(f"{v} {k}" for k, v in counts.items()) + " published"))
    dump = os.path.join(pg_bin, "pg_dump") if pg_bin else shutil.which("pg_dump")
    if dump and os.path.isfile(dump):
        out.append(backup_tooling(dump, server_major))
    else:
        out.append((WARN, "backup tooling", "pg_dump not on PATH — `comfyfarm backup` needs it (--pg-bin <dir>)"))
    return out


def render(results) -> str:
    width = max(len(t) for _, t, _ in results) if results else 0
    return "\n".join(f"{st:4} {t.ljust(width)}  {d}" for st, t, d in results)
