"""Holding dispatch for the whole farm (docs/design/19, D-56): no job goes to a worker while a hold is on; jobs already
on a worker run to the end, new jobs queue. One row in `settings`, so every router of a farm sees the same hold."""
from __future__ import annotations

import datetime as dt

KEY = "dispatch_hold"
IN_FORCE = "(value->>'until' IS NULL OR (value->>'until')::timestamptz > now())"   # a passed `until` no longer holds
# The SQL test the dispatcher uses (tick and claim transaction).
ACTIVE = f"EXISTS (SELECT 1 FROM settings WHERE key = 'dispatch_hold' AND {IN_FORCE})"
MAX_MINUTES = 7 * 24 * 60


async def current(c) -> dict | None:
    """The hold in force, or None (none, or one whose `until` has passed)."""
    v = await c.fetchval(f"SELECT value FROM settings WHERE key = $1 AND {IN_FORCE}", KEY)
    return dict(v) if v is not None else None


async def hold(c, who: str, reason: str | None = None, minutes: int | None = None) -> dict:
    """Hold dispatch (again: the reason and `until` change, `since` stays — it is when the farm stopped)."""
    from ..rollout.engine import audit
    now = await c.fetchval("SELECT now()")
    before = await current(c)
    until = (now + dt.timedelta(minutes=minutes)).isoformat() if minutes else None
    value = {"since": before["since"] if before else now.isoformat(), "by": who, "reason": (reason or "").strip()[:500] or None,
             "until": until}
    await c.execute("INSERT INTO settings(key, value, updated_by) VALUES ($1, $2, $3) ON CONFLICT (key) "
                    "DO UPDATE SET value = excluded.value, updated_at = now(), updated_by = excluded.updated_by", KEY, value, who)
    await audit(c, who, "dispatch held" if before is None else "dispatch hold changed", None,
                reason=value["reason"], until=until)
    return value


async def release(c, who: str) -> bool:
    """End the hold. False if there was none in force (an expired one is cleared too, without a second entry)."""
    from ..rollout.engine import audit
    was = await current(c)
    await c.execute("DELETE FROM settings WHERE key = $1", KEY)
    if was is None:
        return False
    await audit(c, who, "dispatch released", None, held_since=was["since"], held_by=was["by"])
    await c.execute("NOTIFY comfyfarm_wake")
    return True


async def blocks(c) -> bool:
    """The dispatcher's tick: is dispatch held? One indexed read when there is no hold (every tick, every router); a
    hold whose `until` has passed is ended here."""
    row = await c.fetchrow("SELECT (value->>'until') IS NOT NULL AND (value->>'until')::timestamptz <= now() AS over "
                           "FROM settings WHERE key = $1", KEY)
    if row is None:
        return False
    if row["over"]:
        await expire(c)
        return False
    return True


async def expire(c) -> bool:
    """A hold whose `until` has passed ends here (the dispatcher's tick): one router's DELETE wins, one audit entry."""
    from ..rollout.engine import audit
    v = await c.fetchval("DELETE FROM settings WHERE key = $1 AND value->>'until' IS NOT NULL "
                         "AND (value->>'until')::timestamptz <= now() RETURNING value", KEY)
    if v is None:
        return False
    v = dict(v)
    await audit(c, "comfyfarm", "dispatch hold expired", None, held_since=v.get("since"), held_by=v.get("by"),
                until=v.get("until"))
    return True
