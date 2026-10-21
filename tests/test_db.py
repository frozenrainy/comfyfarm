"""The database layer: a slow statement is "busy", not a crash (field, dev.14: a first start on one HDD made a commit
wait over 30 s and the health loop printed a traceback)."""
import logging

import pytest

from comfyfarm import db as dbm


async def test_a_statement_that_times_out_is_busy_not_a_crash(dsn):
    d = dbm.Database(dsn, min_size=1, max_size=2)
    await d.start()
    try:
        with pytest.raises(dbm.Slow, match="took over 30 s — its disk is busy") as e:
            async with d.conn() as c:
                await c.fetchval("SELECT pg_sleep(2)", timeout=0.3)        # what command_timeout does after 30 s
        assert isinstance(e.value, dbm.Busy)                               # every loop and API handler knows Busy
        with pytest.raises(TimeoutError) as e:                             # someone else's timeout stays what it is
            async with d.conn() as c:
                raise TimeoutError("an HTTP wait, not the database")
        assert not isinstance(e.value, dbm.Busy)
        async with d.conn() as c:                                          # the connection is fine afterwards
            assert await c.fetchval("SELECT 1") == 1
    finally:
        await d.close()


def test_a_busy_database_is_one_line_a_minute(caplog, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(dbm.time, "monotonic", lambda: clock[0])
    busy = dbm.BusyLog(logging.getLogger("t"), "health")
    with caplog.at_level(logging.WARNING, logger="t"):
        for _ in range(12):                                                # a tick every 5 s for a minute
            busy(dbm.Slow("a database statement took over 30 s"))
            clock[0] += 5
        busy(dbm.Slow("a database statement took over 30 s"))
    lines = [r.getMessage() for r in caplog.records]
    assert lines == ["health: a database statement took over 30 s — this round is skipped",
                     "health: a database statement took over 30 s — this round is skipped "
                     "(11 more round(s) skipped since the last line)"]
