"""Group permissions (D-34). A user may use the worker groups granted to them; admins and the development identity may
use all (the development identity too, for names without an account). Every queued job carries `allowed_groups` (NULL = any) so the dispatcher and the monitoring numbers apply the
same rule without knowing about users; a permission change re-stamps the owner's queued jobs at once.
"""
from __future__ import annotations


async def user_row(c, username: str):
    return await c.fetchrow("SELECT username, roles, disabled_at, password_hash IS NOT NULL AS local, prefs, session_epoch "
                            "FROM users WHERE username = $1", username)


async def allowed_groups(c, username: str, *, admin: bool, dev: bool) -> list[int] | None:
    """Groups `username` may use; None = all. Under the development identity a name without an account is
    unrestricted (single-user laptop); accounts keep their permissions there too."""
    if admin:
        return None
    if dev and await c.fetchval("SELECT 1 FROM users WHERE username = $1", username) is None:
        return None
    return sorted(r["group_id"] for r in await c.fetch("SELECT group_id FROM user_groups WHERE username = $1", username))


async def owner_allowed(c, owner: str, fallback):
    """Permissions of a job's owner from the ledger (for work done on the owner's behalf, e.g. an admin retrying it).
    Owners without a user row (development identity) keep `fallback`."""
    row = await user_row(c, owner)
    if row is None:
        return fallback
    if "admin" in row["roles"]:
        return None
    return sorted(r["group_id"] for r in await c.fetch("SELECT group_id FROM user_groups WHERE username = $1", owner))


def refusal(allowed: list[int] | None, group_id: int | None, who: str = "you") -> str | None:
    """Why a job with these permissions may not be queued, or None. A job nobody may take would wait forever, so
    submit and retry refuse it instead of stamping `allowed_groups = '{}'`."""
    if allowed is None:
        return None
    if not allowed:
        return f"{who} may not use any worker group; ask an admin"
    if group_id is not None and group_id not in allowed:
        return f"{who} may not use this worker group"
    return None


OWNER_LOCK_SPACE = 0x63664F57   # first key of the per-owner advisory locks ("cfOW"); the second is hashtext(owner)


async def lock_owner(c, owner: str, *, exclusive: bool = False):
    """Inside the caller's transaction. Stamping a new job (shared) and re-stamping an owner's queued jobs (exclusive)
    exclude each other: otherwise a job stamped from permissions read just before a change, and committed just after
    restamp's UPDATE, would keep the old permissions for good."""
    fn = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    await c.execute(f"SELECT {fn}($1, hashtext($2))", OWNER_LOCK_SPACE, owner)


async def restamp(c, username: str) -> int:
    """After a change to `username`'s groups or admin role: queued jobs follow the new permissions immediately."""
    await lock_owner(c, username, exclusive=True)
    allowed = await owner_allowed(c, username, None)
    return int((await c.execute("UPDATE jobs SET allowed_groups = $2 WHERE owner = $1 AND status IN ('pending','paused')",
                                username, allowed)).split()[-1])
