"""comfyfarm command line.

  comfyfarm serve [--host 0.0.0.0] [--port 8188] [--log-dir DIR]     run the router
  comfyfarm add-user <name> [--admin] [--group NAME ...]              create/update a local account (password on stdin)
  comfyfarm backup <dir> [--user-dir PATH] [--pg-bin DIR]              ledger dump + user data + artifact list
  comfyfarm restore <backup-dir> --yes [--user-dir PATH] [--pg-bin DIR]
  comfyfarm doctor [--ui URL]                                          health of ledger, workers, groups, storage, disk

Configuration comes from the environment: COMFYFARM_DSN (required), COMFYFARM_INSTANCE, COMFYFARM_GATEWAY_SECRET,
COMFYFARM_DEFAULT_GROUP, COMFYFARM_ARTIFACTS_DIR, COMFYFARM_DEV_IDENTITY (loopback development only).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import getpass
import json
import os
import shutil
import subprocess
import sys
import tarfile

from .core import access
from .core.settings import Settings
from .db import Database
from .identity import hash_password

ENV_PREFIX = "COMFYFARM_"


def settings_from_env(env=None) -> Settings:
    env = env or os.environ
    dsn = env.get("COMFYFARM_DSN") or sys.exit("COMFYFARM_DSN is not set (postgresql://user:pass@host/db)")
    kw = {"dsn": dsn}
    for key, attr in (("INSTANCE", "instance"), ("GATEWAY_SECRET", "gateway_secret"), ("DEFAULT_GROUP", "default_group"),
                      ("ARTIFACTS_DIR", "artifacts_dir"), ("DEV_IDENTITY", "dev_identity")):
        if env.get(ENV_PREFIX + key):
            kw[attr] = env[ENV_PREFIX + key]
    if env.get("COMFYFARM_DEV_ADMINS"):
        kw["dev_admins"] = tuple(x.strip() for x in env["COMFYFARM_DEV_ADMINS"].split(",") if x.strip())
    return Settings(**kw)


async def add_user(dsn: str, name: str, password: str, admin: bool, groups: list[str] = ()):
    db = Database(dsn, min_size=1, max_size=2)
    await db.start()
    try:
        async with db.conn() as c:
            async with c.transaction():
                ids = [await c.fetchval("SELECT id FROM worker_groups WHERE name = $1", g) for g in groups]
                if None in ids:
                    raise SystemExit(f"unknown group(s): {[g for g, i in zip(groups, ids) if i is None]}")
                await c.execute("INSERT INTO users(username, password_hash, roles) VALUES ($1, $2, $3) "
                                "ON CONFLICT (username) DO UPDATE SET password_hash = excluded.password_hash, "
                                "roles = excluded.roles, session_epoch = users.session_epoch + 1",
                                name, hash_password(password), ["admin"] if admin else [])
                await c.executemany("INSERT INTO user_groups(username, group_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                                    [(name, i) for i in ids])
                await access.restamp(c, name)
    finally:
        await db.close()


# ---- backup / restore -------------------------------------------------------------------------------------------------
def pg_tool(name: str, pg_bin: str | None) -> str:
    path = os.path.join(pg_bin, name) if pg_bin else shutil.which(name)
    if not path or not os.path.isfile(path):
        sys.exit(f"{name} not found — install the PostgreSQL client tools or pass --pg-bin <dir>")
    return path


def backup(dsn: str, out_dir: str, user_dir: str | None, artifacts_dir: str | None, pg_bin: str | None) -> str:
    """One directory per backup: the ledger (pg_dump custom format), the UI worker's user data (workflows,
    settings), the list of published artifacts, and a manifest. Secrets are deliberately not included: the gateway
    secret, session key, ssh keys and agent token files live in the service configuration and are backed up by the
    site's means — a backup that carries them is a second copy to protect."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = os.path.join(out_dir, f"comfyfarm-backup-{stamp}")
    os.makedirs(dest)
    manifest = {"created": stamp, "contents": []}
    dump = os.path.join(dest, "ledger.dump")
    subprocess.run([pg_tool("pg_dump", pg_bin), "--format=custom", "--no-owner", "--no-privileges", "--file", dump, dsn], check=True)
    manifest["contents"].append({"file": "ledger.dump", "what": "Postgres ledger (pg_dump -Fc)", "bytes": os.path.getsize(dump)})
    if user_dir:
        tar = os.path.join(dest, "userdata.tar.gz")
        with tarfile.open(tar, "w:gz") as t:
            t.add(user_dir, arcname="user")
        manifest["contents"].append({"file": "userdata.tar.gz", "what": f"UI worker user directory ({user_dir})", "bytes": os.path.getsize(tar)})
    if artifacts_dir:
        arts = {}
        for kind in ("core", "venv", "nodes", "frontend"):
            d = os.path.join(artifacts_dir, kind)
            for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
                mp = os.path.join(d, n, "manifest.json")
                if os.path.isfile(os.path.join(d, n, "READY")) and os.path.isfile(mp):
                    with open(mp) as f:
                        arts.setdefault(kind, {})[n] = json.load(f)
        with open(os.path.join(dest, "artifacts.json"), "w") as f:
            json.dump(arts, f, indent=1)
        manifest["contents"].append({"file": "artifacts.json", "what": "names and manifests of published artifacts "
                                     "(the files themselves are re-published from recipes or backed up with the store)"})
    with open(os.path.join(dest, "MANIFEST.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    with open(os.path.join(dest, "README.txt"), "w") as f:
        f.write("ComfyFarm backup. Restore with:  comfyfarm restore <this directory> --yes [--user-dir <UI worker user dir>]\n"
                "Not included (keep them with the service configuration): gateway secret, session key, ssh keys, agent tokens.\n")
    return dest


def restore(dsn: str, backup_dir: str, user_dir: str | None, pg_bin: str | None) -> None:
    dump = os.path.join(backup_dir, "ledger.dump")
    if not os.path.isfile(dump):
        sys.exit(f"{backup_dir} holds no ledger.dump")
    # --clean --if-exists: the live tables are dropped and recreated from the dump; stop the router first
    subprocess.run([pg_tool("pg_restore", pg_bin), "--clean", "--if-exists", "--no-owner", "--no-privileges",
                    "--dbname", dsn, dump], check=True)
    tar = os.path.join(backup_dir, "userdata.tar.gz")
    if user_dir and os.path.isfile(tar):
        with tarfile.open(tar) as t:
            t.extractall(os.path.dirname(user_dir.rstrip("/")) or ".", filter="data")
        extracted = os.path.join(os.path.dirname(user_dir.rstrip("/")) or ".", "user")
        if os.path.abspath(extracted) != os.path.abspath(user_dir):
            if os.path.exists(user_dir):
                shutil.rmtree(user_dir)
            os.rename(extracted, user_dir)


# ---- entry point --------------------------------------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="comfyfarm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the router")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8188)
    s.add_argument("--log-dir", help="also write comfyfarm.log (rotated) and error.log here")
    s.add_argument("--log-level", default="INFO")
    a = sub.add_parser("add-user", help="create or update a local account (password read from stdin)")
    a.add_argument("name")
    a.add_argument("--admin", action="store_true")
    a.add_argument("--group", action="append", default=[], help="worker group the user may use (repeatable)")
    b = sub.add_parser("backup", help="ledger dump + user data + artifact list into <dir>/comfyfarm-backup-<time>/")
    b.add_argument("dir")
    b.add_argument("--user-dir", help="the UI worker's user directory (workflows, settings)")
    b.add_argument("--pg-bin", help="directory holding pg_dump/pg_restore if not on PATH")
    r = sub.add_parser("restore", help="restore a backup directory (stop the router first)")
    r.add_argument("backup_dir")
    r.add_argument("--yes", action="store_true", help="really replace the live ledger")
    r.add_argument("--user-dir")
    r.add_argument("--pg-bin")
    d = sub.add_parser("doctor", help="check ledger, workers, groups, storage, disk")
    d.add_argument("--ui", help="UI worker URL: verifies that GPU workers see files uploaded there (shared storage)")
    d.add_argument("--pg-bin")
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn
        from .app import create_app
        from .logs import setup
        setup(args.log_dir, args.log_level)
        uvicorn.run(create_app(settings_from_env()), host=args.host, port=args.port, log_config=None)
        return
    st = settings_from_env()
    if args.cmd == "add-user":
        pw = sys.stdin.readline().rstrip("\n") if not sys.stdin.isatty() else getpass.getpass()
        asyncio.run(add_user(st.dsn, args.name, pw, args.admin, args.group))
        print(f"user {args.name} saved")
    elif args.cmd == "backup":
        dest = backup(st.dsn, args.dir, args.user_dir, st.artifacts_dir, args.pg_bin)
        print(f"backup written to {dest}")
    elif args.cmd == "restore":
        if not args.yes:
            sys.exit("this replaces the live ledger; stop the router, then run again with --yes")
        restore(st.dsn, args.backup_dir, args.user_dir, args.pg_bin)
        print("restored — start the router")
    elif args.cmd == "doctor":
        from .doctor import render, run
        results = asyncio.run(run(st, args.ui, args.pg_bin))
        print(render(results))
        sys.exit(1 if any(x[0] == "FAIL" for x in results) else 0)


if __name__ == "__main__":
    main()
