"""Database access. The only way to get a connection is `Database.conn()`, which always bounds the wait (D-21):
asyncpg's pool.acquire() waits forever by default, which is exactly how the original system's loops went silent."""
from __future__ import annotations

import contextlib
import importlib.resources
import logging
import re
import time

import asyncpg

log = logging.getLogger(__name__)
ACQUIRE_TIMEOUT = 5.0
COMMAND_TIMEOUT = 30.0
MIGRATION_LOCK = 0x636F6D6679  # advisory lock id ("comfy")
MIGRATION_NAME = re.compile(r"^\d{4}_[A-Za-z0-9_]+\.sql$")


class Busy(Exception):
    """The database pool is exhausted — callers answer 503 + Retry-After instead of waiting."""


class Slow(Busy):
    """A statement waited longer than COMMAND_TIMEOUT: the database, or the disk under it, is overloaded (a build
    writing tens of thousands of files to the same HDD made a commit wait over 30 s). Handled as Busy: a loop skips a
    round, an API call answers 503 — not a traceback, which reads like a bug."""


def _from_asyncpg(e: BaseException) -> bool:
    tb = e.__traceback__
    while tb is not None:
        if "asyncpg" in tb.tb_frame.f_code.co_filename:
            return True
        tb = tb.tb_next
    return False


class BusyLog:
    """One line per minute for a loop that keeps meeting a busy database, with how many rounds it skipped since."""

    def __init__(self, logger: logging.Logger, what: str, every: float = 60.0):
        self.log, self.what, self.every = logger, what, every
        self.last, self.skipped = -every, 0

    def __call__(self, e: Exception) -> None:
        now = time.monotonic()
        if now - self.last < self.every:
            self.skipped += 1
            return
        more = f" ({self.skipped} more round(s) skipped since the last line)" if self.skipped else ""
        self.log.warning("%s: %s — this round is skipped%s", self.what, e, more)
        self.last, self.skipped = now, 0


class Database:
    def __init__(self, dsn: str, *, min_size: int = 2, max_size: int = 20):
        self.dsn, self.min_size, self.max_size = dsn, min_size, max_size
        self.pool: asyncpg.Pool | None = None

    async def start(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=self.min_size, max_size=self.max_size,
                                              init=self._init_conn, command_timeout=COMMAND_TIMEOUT)
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
        except TimeoutError as e:
            if _from_asyncpg(e):                  # a statement's own timeout, not someone else's wait_for
                raise Slow(f"a database statement took over {COMMAND_TIMEOUT:.0f} s — its disk is busy (a build, "
                           "copies?) or the database is overloaded") from e
            raise
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
