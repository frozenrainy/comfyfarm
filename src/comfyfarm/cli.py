"""comfyfarm command line.

  comfyfarm serve [--host 0.0.0.0] [--port 8188] [--log-dir DIR]     run the router
  comfyfarm add-user <name> [--admin|--no-admin] [--group NAME ...]   create/update a local account (password on stdin;
                                                                       an existing account keeps its role unless told)
  comfyfarm backup <dir> [--user-dir PATH] [--pg-bin DIR]              ledger dump + user data + artifact list
  comfyfarm restore <backup-dir> --yes [--user-dir PATH] [--pg-bin DIR]
  comfyfarm doctor [--ui URL]                                          health of ledger, workers, groups, storage, disk
  comfyfarm prepare <state-dir> [--chown DIR]                          easy deployment: secrets/, keys/, backups/ in one
                                                                       directory (made once, kept on every later run)
  comfyfarm secrets [--write DIR]                                      fresh gateway secret and session key (--write: also a
                                                                       database password, as files for compose secrets)

Configuration comes from the environment: COMFYFARM_DSN (required), COMFYFARM_INSTANCE, COMFYFARM_GATEWAY_SECRET,
COMFYFARM_DEFAULT_GROUP, COMFYFARM_ARTIFACTS_DIR, COMFYFARM_KEYS_DIR (ssh keys and agent token files the restart
backends may reference; default /etc/comfyfarm/keys), COMFYFARM_DEV_IDENTITY (loopback development only).
Secrets may come from files instead: PGPASSWORD_FILE, COMFYFARM_GATEWAY_SECRET_FILE (compose secrets).
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
import tempfile
from urllib.parse import unquote, urlsplit, urlunsplit

from .core import access
from .core.settings import Settings
from .db import Database
from .envfile import load_secret_files
from .identity import hash_password

ENV_PREFIX = "COMFYFARM_"


def settings_from_env(env=None) -> Settings:
    env = env or os.environ
    dsn = env.get("COMFYFARM_DSN") or sys.exit("COMFYFARM_DSN is not set (postgresql://user:pass@host/db)")
    kw = {"dsn": dsn}
    for key, attr in (("INSTANCE", "instance"), ("GATEWAY_SECRET", "gateway_secret"), ("DEFAULT_GROUP", "default_group"),
                      ("ARTIFACTS_DIR", "artifacts_dir"), ("DEV_IDENTITY", "dev_identity"), ("KEYS_DIR", "keys_dir"),
                      ("BUILDS_DIR", "builds_dir"), ("UI_SERVICE", "ui_service"), ("UI_AGENT", "ui_agent"),
                      ("UI_HOME", "ui_home"), ("UI_SHARED", "ui_shared"), ("FIRST_RECIPE", "first_recipe"),
                      ("PUBLIC_ROUTER", "public_router"), ("MODELS_DIR", "models_dir")):
        if env.get(ENV_PREFIX + key):
            kw[attr] = env[ENV_PREFIX + key]
    if env.get("COMFYFARM_DEV_ADMINS"):
        kw["dev_admins"] = tuple(x.strip() for x in env["COMFYFARM_DEV_ADMINS"].split(",") if x.strip())
    return Settings(**kw)


async def add_user(dsn: str, name: str, password: str, admin: bool | None, groups: list[str] = ()):
    """admin None keeps an existing account's role (a password reset must not quietly demote the last admin)."""
    from .api.account import password_problem
    from .api.admin import USERNAME
    if not USERNAME.match(name):
        raise SystemExit("user name: letters, digits and . _ @ - (max 64), starting with a letter or digit")
    if problem := password_problem(password):
        raise SystemExit(f"password: {problem}")
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
                                "roles = CASE WHEN $4::bool IS NULL THEN users.roles WHEN $4 THEN "
                                "array(SELECT DISTINCT unnest(users.roles || '{admin}'::text[])) "
                                "ELSE array_remove(users.roles, 'admin') END, "
                                "session_epoch = next_session_epoch(users.session_epoch)",
                                name, hash_password(password), ["admin"] if admin else [], admin)
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


def pg_run(tool: str, args: list[str], dsn: str) -> None:
    """Run a PostgreSQL client tool against `dsn` without the password on its command line: it would show in `ps`
    and in the error when the tool fails. The password goes through PGPASSWORD instead."""
    env, u = dict(os.environ), urlsplit(dsn)
    if u.scheme.startswith("postgres") and u.password is not None:
        env["PGPASSWORD"] = unquote(u.password)
        h = u.hostname or ""
        host = (f"[{h}]" if ":" in h else h) + (f":{u.port}" if u.port else "")
        user = f"{u.username}@" if u.username else ""
        dsn = urlunsplit((u.scheme, user + host, u.path, u.query, u.fragment))
    r = subprocess.run([tool, *args, "--dbname", dsn], env=env)
    if r.returncode:
        sys.exit(f"{os.path.basename(tool)} failed (exit {r.returncode}) — its message is above")


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
    pg_run(pg_tool("pg_dump", pg_bin), ["--format=custom", "--no-owner", "--no-privileges", "--file", dump], dsn)
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


PARKED = "comfyfarm_before_restore"


def _sql(dsn: str, *statements: str) -> None:
    """One transaction; run on its own thread so callers inside an event loop can use it too."""
    import asyncpg
    from concurrent.futures import ThreadPoolExecutor

    async def go():
        c = await asyncpg.connect(dsn)
        try:
            async with c.transaction():
                for q in statements:
                    await c.execute(q)
        finally:
            await c.close()
    with ThreadPoolExecutor(1) as ex:
        ex.submit(asyncio.run, go()).result()


def restore(dsn: str, backup_dir: str, user_dir: str | None, pg_bin: str | None) -> None:
    """The ledger is restored into an empty schema, not over the live one: `pg_restore --clean` drops only what the
    dump holds, so tables of migrations newer than the backup would survive and the router could not start (or the
    drop would fail on their foreign keys). The live schema is parked until the restore succeeded and put back if it
    did not; the router applies the newer migrations to the restored ledger when it starts."""
    dump = os.path.join(backup_dir, "ledger.dump")
    if not os.path.isfile(dump):
        sys.exit(f"{backup_dir} holds no ledger.dump")
    tar = os.path.join(backup_dir, "userdata.tar.gz")
    if user_dir and not os.path.isfile(tar):
        sys.exit(f"{backup_dir} holds no userdata.tar.gz — leave out --user-dir to restore the ledger only")
    try:
        _sql(dsn, f"ALTER SCHEMA public RENAME TO {PARKED}", "CREATE SCHEMA public")
    except Exception as e:                          # e.g. a parked schema left by an interrupted restore
        sys.exit(f"cannot set the live ledger aside ({type(e).__name__}: {e}) — if a schema {PARKED} exists, an earlier "
                 f"restore was interrupted: inspect it, then drop it or rename it back to public")
    try:
        pg_run(pg_tool("pg_restore", pg_bin), ["--no-owner", "--no-privileges", "--single-transaction", dump], dsn)
    except SystemExit:
        _sql(dsn, "DROP SCHEMA public CASCADE", f"ALTER SCHEMA {PARKED} RENAME TO public")
        raise
    _sql(dsn, f"DROP SCHEMA {PARKED} CASCADE")
    if user_dir:
        restore_user_dir(tar, user_dir)


def restore_user_dir(tar: str, user_dir: str) -> None:
    """Unpacked next to the target under a fresh name, then swapped in: the directory ends up exactly as in the
    backup (not merged with what is there now), nothing else in the parent is touched, and the current content is
    kept as <user_dir>.before-restore-<time> until someone deletes it."""
    user_dir = os.path.abspath(user_dir.rstrip("/"))
    parent = os.path.dirname(user_dir)
    stage = tempfile.mkdtemp(prefix=".comfyfarm-restore-", dir=parent)
    try:
        with tarfile.open(tar) as t:
            t.extractall(stage, filter="data")
        src = os.path.join(stage, "user")
        if not os.path.isdir(src):
            sys.exit(f"{tar} holds no user/ directory")
        if os.path.exists(user_dir):
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            os.rename(user_dir, f"{user_dir}.before-restore-{stamp}")
        os.rename(src, user_dir)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# ---- entry point --------------------------------------------------------------------------------------------------------
SECRET_FILES = {"db_password": 24, "gateway_secret": 32, "session_key": 48}   # file → bytes of randomness


def write_secret_files(directory: str) -> list[str]:
    """Fresh secrets as files, readable by the containers that mount them (0444 — keep DIR itself 0700). An existing
    file is never replaced: a new database password would not match the initialised database, and a new session key
    logs everyone out."""
    import secrets as _s
    written = []
    for name, size in SECRET_FILES.items():
        path = os.path.join(directory, name)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as f:
            f.write(_s.token_urlsafe(size) + "\n")
        os.chmod(path, 0o444)                                       # the umask must not take the containers' read away
        written.append(name)
    return written


SERVICE_OWNER = (10001, 10001)               # the image's account (deploy/Dockerfile)


def give_to_service(paths: list[str], owner: tuple[int, int] = SERVICE_OWNER) -> None:
    """Run as root (the easy `init`): hand the directories the service writes to its account, recursively. A bind
    mount (./state instead of a named volume) is created by Docker as root, so the service could not write there."""
    for top in paths:
        for path in [top] + [os.path.join(d, n) for d, dirs, files in os.walk(top) for n in dirs + files]:
            try:
                os.lchown(path, *owner)
            except FileNotFoundError:
                continue                                   # the running router rotated or removed it meanwhile
            except PermissionError:
                sys.exit(f"cannot give {path} to uid {owner[0]} (an NFS export with root_squash?) — "
                         f"chown -R {owner[0]}:{owner[1]} it where it is stored, or use a Docker volume")


BUILDER_OWNER = (10002, 10002)              # the builder service: owns the artifact store (deploy/easy/compose.yaml)


def take_dir(path: str, owner: tuple[int, int], ours: tuple[str, ...] = ()) -> str:
    """Give a directory to an account when it is new (empty — Docker made it for a bind mount) or already that
    account's. A directory holding someone else's files is left alone: never another owner's data. `ours`: empty
    subdirectories the farm makes itself (a nested bind mount's, made by Docker before this runs) do not count."""
    os.makedirs(path, exist_ok=True)
    st = os.stat(path)
    if (st.st_uid, st.st_gid) == owner:
        return f"{path}: uid {owner[0]}'s"
    if [n for n in os.listdir(path) if not (n in ours and os.path.isdir(os.path.join(path, n))
                                            and not os.listdir(os.path.join(path, n)))]:
        return (f"{path}: LEFT ALONE — it holds files and belongs to uid {st.st_uid}, not {owner[0]}; "
                f"if it is meant for this farm: chown -R {owner[0]}:{owner[1]} {path}")
    give_to_service([path], owner)
    return f"{path}: given to uid {owner[0]}"


def prepare_compose(keys: str, builds: str | None, ui_token: str | None, art: str | None, shared: str | None,
                    builder_home: str | None, ui_home: str | None, farm_owner: tuple[int, int],
                    models: str | None = None) -> list[str]:
    """The compose deployment's builder and UI worker container (docs/design/15), made ready by `init` (root):
    the request/status directories, the UI worker's agent token, and who owns the store and the shared storage."""
    said = []
    if builds:
        # archives: node pack archives the router files for the builder to unpack (docs/design/18)
        for sub, owner in (("requests", SERVICE_OWNER), ("status", BUILDER_OWNER), ("archives", SERVICE_OWNER)):
            path = os.path.join(builds, sub)
            os.makedirs(path, exist_ok=True)
            os.chmod(path, 0o755)                          # each side reads the other's, writes only its own
            give_to_service([path], owner)
        os.chmod(builds, 0o755)
    if ui_token:
        import secrets as _s
        keep = os.path.join(keys, "ui_agent_token")                 # the router's keys directory
        token = open(keep).read().strip() if os.path.exists(keep) else _s.token_urlsafe(32)
        for path, owner in ((keep, SERVICE_OWNER), (os.path.join(ui_token, "token"), farm_owner)):
            with open(path, "w") as f:
                f.write(token + "\n")
            os.chmod(path, 0o400)
            os.chown(path, *owner)
        os.chmod(ui_token, 0o755)
    for path, owner in ((art, BUILDER_OWNER), (models, BUILDER_OWNER), (builder_home, BUILDER_OWNER), (ui_home, farm_owner)):
        if path:
            said.append(take_dir(path, owner, (".uploads", "inbox") if path == models else ()))
    if models:
        # the router's uploads, until the builder has them (docs/design/18), and the inbox an operator copies into
        for sub, owner in ((".uploads", SERVICE_OWNER), ("inbox", BUILDER_OWNER)):
            path = os.path.join(models, sub)
            os.makedirs(path, exist_ok=True)
            os.chmod(path, 0o755)
            os.chown(path, *owner)
    if shared:
        os.makedirs(shared, exist_ok=True)
        for sub in ("input", "output", "user"):
            said.append(take_dir(os.path.join(shared, sub), farm_owner))
    return said


def prepare_state(root: str, chown: list[str] = ()) -> list[str]:
    """The easy deployment's state directory (a volume or a bind mount): secrets/, keys/ and backups/, 0700, the
    secrets 0400 — the owner's alone (Postgres reads its file as root). Run by the compose `init` service before
    anything else starts; idempotent. As root it also gives the state and `chown` directories (the logs) to the
    service's account — then a bind mount works as well as a named volume."""
    made = []
    for sub, mode in (("secrets", 0o700), ("keys", 0o700), ("backups", 0o700)):
        path = os.path.join(root, sub)
        if not os.path.isdir(path):
            os.makedirs(path)
            made.append(sub + "/")
        os.chmod(path, mode)
    made += ["secrets/" + n for n in write_secret_files(os.path.join(root, "secrets"))]
    # With a bind mount every account on the host would otherwise read the secrets (and the setup code in the logs):
    # the state and the logs are the service's alone. Postgres reads its password file as root before it drops to
    # its own user, so 0400 for the owner is enough for every container.
    for name in SECRET_FILES:
        os.chmod(os.path.join(root, "secrets", name), 0o400)
    for d in (root, *chown):
        if os.path.isdir(d):
            os.chmod(d, 0o700)
    if os.geteuid() == 0:
        give_to_service([root, *chown])
    return made


def main(argv=None):
    ap = argparse.ArgumentParser(prog="comfyfarm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the router")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8188)
    s.add_argument("--log-dir", help="also write router.log (rotated) and router-error.log here")
    s.add_argument("--log-level", default="INFO")
    a = sub.add_parser("add-user", help="create or update a local account (password read from stdin)")
    a.add_argument("name")
    adm = a.add_mutually_exclusive_group()
    adm.add_argument("--admin", action="store_true", default=None, help="make the account an administrator")
    adm.add_argument("--no-admin", dest="admin", action="store_false", help="remove the administrator role")
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
    pr = sub.add_parser("prepare", help="easy deployment: make the state directory (secrets, keys, backups) once")
    pr.add_argument("dir", help="the state directory, or - for none (the strict deployment keeps its secrets on the host)")
    pr.add_argument("--keys", help="the router's keys directory (default: <dir>/keys)")
    pr.add_argument("--chown", action="append", default=[], metavar="DIR",
                    help="as root: also give DIR (recursively) to the service account, e.g. the log directory")
    pr.add_argument("--builds", help="the builder's request/status directory (docs/design/15)")
    pr.add_argument("--ui-token", help="the UI worker container's token directory")
    pr.add_argument("--art", help="the artifact store: the builder's when it is new")
    pr.add_argument("--models", help="the model store (docs/design/17): the builder's when it is new")
    pr.add_argument("--shared", help="the shared storage: input/output/user, the farm account's when new")
    pr.add_argument("--builder-home", help="the builder's own directory (uv's cache and interpreters)")
    pr.add_argument("--ui-home", help="the UI worker container's own directory")
    pr.add_argument("--farm-uid", type=int, default=47800, help="the uid of the workers' service account (default 47800)")
    sub.add_parser("builder", help="the compose deployment's builder (docs/design/15)", add_help=False)
    sub.add_parser("ui-worker", help="the compose deployment's UI worker container (docs/design/15)", add_help=False)
    sc = sub.add_parser("secrets", help="print a fresh COMFYFARM_GATEWAY_SECRET and COMFYFARM_SESSION_KEY")
    sc.add_argument("--write", metavar="DIR", help="write db_password, gateway_secret and session_key into DIR instead "
                                                   "(files for compose secrets; existing files are kept)")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["builder"]:
        from . import builder
        sys.exit(builder.main(argv[1:]))
    if argv[:1] == ["ui-worker"]:
        from . import uiworker
        sys.exit(uiworker.main(argv[1:]))
    args = ap.parse_args(argv)

    if args.cmd == "prepare":
        if args.dir != "-":
            made = prepare_state(args.dir, args.chown)
            print("state: " + (", ".join(made) + " made" if made else "already prepared, nothing changed"))
        if args.dir == "-" and not (args.art and args.shared):
            sys.exit("prepare: COMFYFARM_ARTIFACTS_MOUNT and COMFYFARM_SHARED must be set for the builder and the UI worker "
                     "(.env, docs/guide/install.md)")
        if os.geteuid() == 0 and any((args.builds, args.ui_token, args.art, args.shared, args.builder_home, args.ui_home,
                                      args.models)):
            keys = args.keys or os.path.join(args.dir, "keys")
            for line in prepare_compose(keys, args.builds, args.ui_token, args.art, args.shared, args.builder_home,
                                        args.ui_home, (args.farm_uid, args.farm_uid), args.models):
                print(line)
        return
    if args.cmd == "secrets":
        if args.write:
            for name in write_secret_files(args.write):
                print(f"wrote {os.path.join(args.write, name)}")
            return
        import secrets as _s
        print(f"COMFYFARM_GATEWAY_SECRET={_s.token_urlsafe(32)}")
        print(f"COMFYFARM_SESSION_KEY={_s.token_urlsafe(48)}")
        return
    load_secret_files()
    if args.cmd == "serve":
        import uvicorn
        if os.environ.get("COMFYFARM_DEV_IDENTITY") and args.host not in ("127.0.0.1", "::1", "localhost"):
            sys.exit("COMFYFARM_DEV_IDENTITY makes every request that user: it is for a laptop, --host 127.0.0.1 only")
        from .app import create_app
        from .logs import setup
        setup(args.log_dir, args.log_level)
        # proxy_headers=False: the peer address stays the connection's own (X-Forwarded-For is client-written)
        uvicorn.run(create_app(settings_from_env()), host=args.host, port=args.port, log_config=None, proxy_headers=False)
        return
    st = settings_from_env()
    if args.cmd == "add-user":
        pw = sys.stdin.readline().rstrip("\n") if not sys.stdin.isatty() else getpass.getpass()
        asyncio.run(add_user(st.dsn, args.name, pw, args.admin, args.group))
        print(f"user {args.name} saved")
    elif args.cmd == "backup":
        dest = backup(st.dsn, args.dir, args.user_dir, st.artifacts_dir, args.pg_bin)
        print(f"backup written to {dest}")
        if not args.user_dir:
            print("note: the users' workflows and settings (the UI worker's user directory) are NOT in this backup — "
                  "pass --user-dir (with containers: COMFYFARM_USER_DIR in .env, then --user-dir /userdata), or back that "
                  "directory up with the shared storage (backup.md)")
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
