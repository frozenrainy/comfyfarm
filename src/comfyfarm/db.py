"""Database access. The only way to get a connection is `Database.conn()`, which always bounds the wait (D-21):
asyncpg's pool.acquire() waits forever by default, which is exactly how the original system's loops went silent."""
from __future__ import annotations

import contextlib
import importlib.resources
import logging
import re

import asyncpg

log = logging.getLogger(__name__)
ACQUIRE_TIMEOUT = 5.0
MIGRATION_LOCK = 0x636F6D6679  # advisory lock id ("comfy")
MIGRATION_NAME = re.compile(r"^\d{4}_[A-Za-z0-9_]+\.sql$")


class Busy(Exception):
    """The database pool is exhausted — callers answer 503 + Retry-After instead of waiting."""


class Database:
    def __init__(self, dsn: str, *, min_size: int = 2, max_size: int = 20):
        self.dsn, self.min_size, self.max_size = dsn, min_size, max_size
        self.pool: asyncpg.Pool | None = None

    async def start(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=self.min_size, max_size=self.max_size,
                                              init=self._init_conn, command_timeout=30)
        await self.migrate()

    @staticmethod
    async def _init_conn(conn):
        await conn.execute("SET TIME ZONE 'UTC'")          # one clock (D-25)
        await conn.set_type_codec("jsonb", encoder=_json_dumps, decoder=_json_loads, schema="pg_catalog")

    async def close(self):
        if self.pool:
            await self.pool.close()

    @contextlib.asynccontextmanager
    async def conn(self, timeout: float = ACQUIRE_TIMEOUT):
        try:
            c = await self.pool.acquire(timeout=timeout)
        except TimeoutError as e:
            raise Busy("database pool exhausted") from e
        try:
            yield c
        finally:
            await self.pool.release(c)

    async def migrate(self):
        files = migration_files(importlib.resources.files("comfyfarm.migrations").iterdir())
        async with self.conn(timeout=60) as c:
            await c.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK)     # two routers starting at once
            try:
                await c.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                                "(version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
                done = {r["version"] for r in await c.fetch("SELECT version FROM schema_migrations")}
                for f in files:
                    v = int(f.name.split("_", 1)[0])
                    if v in done:
                        continue
                    async with c.transaction():
                        await c.execute(f.read_text())
                        await c.execute("INSERT INTO schema_migrations(version) VALUES ($1)", v)
                    log.info("applied migration %s", f.name)
            finally:
                await c.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK)


def migration_files(entries):
    """Only `NNNN_name.sql`, in version order: a copied tree may carry `._0001_init.sql` (macOS), `0005_x.sql~` or
    similar, and one such file must not stop the router from starting."""
    return sorted((f for f in entries if MIGRATION_NAME.match(f.name)), key=lambda f: f.name)


def _json_dumps(v):
    import json
    return json.dumps(v, separators=(",", ":"))


def _json_loads(s):
    import json
    return json.loads(s)
