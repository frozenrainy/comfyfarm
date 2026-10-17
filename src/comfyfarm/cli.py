"""comfyfarm admin CLI.  python -m comfyfarm.cli add-user <name> [--admin] [--group NAME ...]
(reads COMFYFARM_DSN, password from stdin). Creates or updates the account; the first admin is made this way, everything
else is easier from the dashboard's admin pages."""
import argparse
import asyncio
import getpass
import os
import sys

from .core import access
from .db import Database
from .identity import hash_password


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


def main():
    ap = argparse.ArgumentParser(prog="comfyfarm")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-user")
    a.add_argument("name")
    a.add_argument("--admin", action="store_true")
    a.add_argument("--group", action="append", default=[], help="worker group the user may use (repeatable)")
    args = ap.parse_args()
    dsn = os.environ.get("COMFYFARM_DSN") or sys.exit("COMFYFARM_DSN is not set")
    pw = sys.stdin.readline().rstrip("\n") if not sys.stdin.isatty() else getpass.getpass()
    asyncio.run(add_user(dsn, args.name, pw, args.admin, args.group))
    print(f"user {args.name} saved")


if __name__ == "__main__":
    main()
