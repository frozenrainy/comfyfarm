"""comfyfarm admin CLI.  python -m comfyfarm.cli add-user <name> [--admin]  (reads COMFYFARM_DSN, password from stdin)"""
import argparse
import asyncio
import getpass
import os
import sys

from .db import Database
from .identity import hash_password


async def add_user(dsn: str, name: str, password: str, admin: bool):
    db = Database(dsn, min_size=1, max_size=2)
    await db.start()
    try:
        async with db.conn() as c:
            await c.execute("INSERT INTO users(username, password_hash, roles) VALUES ($1, $2, $3) "
                            "ON CONFLICT (username) DO UPDATE SET password_hash = excluded.password_hash, roles = excluded.roles",
                            name, hash_password(password), ["admin"] if admin else [])
    finally:
        await db.close()


def main():
    ap = argparse.ArgumentParser(prog="comfyfarm")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-user")
    a.add_argument("name")
    a.add_argument("--admin", action="store_true")
    args = ap.parse_args()
    dsn = os.environ.get("COMFYFARM_DSN") or sys.exit("COMFYFARM_DSN is not set")
    pw = sys.stdin.readline().rstrip("\n") if not sys.stdin.isatty() else getpass.getpass()
    asyncio.run(add_user(dsn, args.name, pw, args.admin))
    print(f"user {args.name} saved")


if __name__ == "__main__":
    main()
