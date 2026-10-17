"""Usage statistics over a period of at most MAX_DAYS (dashboard: one user; admin page: everybody).

- jobs are counted in the period they **finished** in;
- `gpu_seconds` is worker occupancy (dispatch → finish, model loading included): what the job cost the farm;
- `avg_run_seconds` is execution only (start → finish) of completed jobs, `avg_wait_seconds` submit → dispatch;
- worker `utilization` is the share of the period a worker held a job, from job intervals clipped to the period
  (jobs still running count up to now).
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_DAYS = 30
TERMINAL = ("completed", "failed", "cancelled")


class BadRange(ValueError):
    pass


def period(days: int | None, start: str | None, end: str | None, tz: str | None,
           max_days: int | None = MAX_DAYS) -> tuple[dt.datetime, dt.datetime, ZoneInfo]:
    """`days` back from now, or calendar dates `start`..`end` (inclusive) in the browser's time zone.
    `max_days=None`: any span (history search)."""
    try:
        zone = ZoneInfo(tz or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        raise BadRange(f"unknown time zone {tz!r}")
    now = dt.datetime.now(dt.timezone.utc)
    if start or end:
        try:
            d0 = dt.date.fromisoformat(start or end)
            d1 = dt.date.fromisoformat(end or start)
        except ValueError:
            raise BadRange("dates must be YYYY-MM-DD")
        if d1 < d0:
            raise BadRange("end is before start")
        if max_days is not None and (d1 - d0).days + 1 > max_days:
            raise BadRange(f"at most {max_days} days")
        since = dt.datetime.combine(d0, dt.time(), zone)
        until = min(dt.datetime.combine(d1 + dt.timedelta(days=1), dt.time(), zone), now)
        if until <= since:
            raise BadRange("the period lies in the future")
        return since, until, zone
    days = days or 7
    if not 1 <= days <= MAX_DAYS:
        raise BadRange(f"days must be 1..{MAX_DAYS}")
    return now - dt.timedelta(days=days), now, zone


def _bucket():
    return {"jobs": 0, "completed": 0, "failed": 0, "cancelled": 0, "gpu_seconds": 0.0,
            "_run": [], "_wait": []}


def _finish(b: dict) -> dict:
    run, wait = b.pop("_run"), b.pop("_wait")
    b["avg_run_seconds"] = sum(run) / len(run) if run else None
    b["avg_wait_seconds"] = sum(wait) / len(wait) if wait else None
    b["gpu_seconds"] = round(b["gpu_seconds"], 1)
    return b


async def compute(c, since: dt.datetime, until: dt.datetime, zone: ZoneInfo, *, owner: str | None = None,
                  groups: list[int] | None = None) -> dict:
    rows = await c.fetch(
        "SELECT j.status, j.owner, j.worker_id, coalesce(w.group_id, j.group_id) AS gid, j.finished_at, "
        "extract(epoch FROM j.finished_at - j.started_at) AS run, "
        "extract(epoch FROM j.finished_at - coalesce(j.dispatched_at, j.started_at)) AS occupied, "
        "extract(epoch FROM coalesce(j.dispatched_at, j.started_at) - j.created_at) AS wait "
        "FROM jobs j LEFT JOIN workers w ON w.id = j.worker_id "
        "WHERE j.status IN ('completed','failed','cancelled') AND j.finished_at >= $1 AND j.finished_at < $2 "
        "AND ($3::text IS NULL OR j.owner = $3) "
        "AND ($4::bigint[] IS NULL OR coalesce(w.group_id, j.group_id) = ANY($4))", since, until, owner, groups)
    total, by_group, by_user, by_worker, daily = _bucket(), {}, {}, {}, {}
    day = since.astimezone(zone).date()
    last = (until - dt.timedelta(microseconds=1)).astimezone(zone).date()
    while day <= last:
        daily[day.isoformat()] = {"completed": 0, "failed": 0, "cancelled": 0}
        day += dt.timedelta(days=1)
    for r in rows:
        targets = [total, by_group.setdefault(r["gid"], _bucket()), by_user.setdefault(r["owner"], _bucket())]
        if r["worker_id"] is not None:
            targets.append(by_worker.setdefault(r["worker_id"], _bucket()))
        for b in targets:
            b["jobs"] += 1
            b[r["status"]] += 1
            if r["occupied"] is not None:
                b["gpu_seconds"] += float(r["occupied"])
            if r["status"] == "completed" and r["run"] is not None:
                b["_run"].append(float(r["run"]))
            if r["wait"] is not None and r["status"] == "completed":
                b["_wait"].append(float(r["wait"]))
        d = daily.get(r["finished_at"].astimezone(zone).date().isoformat())
        if d is not None:
            d[r["status"]] += 1

    span = (until - since).total_seconds()
    busy = {r["worker_id"]: float(r["busy"]) for r in await c.fetch(
        "SELECT j.worker_id, sum(extract(epoch FROM least(coalesce(j.finished_at, now()), $2) "
        "                               - greatest(coalesce(j.dispatched_at, j.started_at), $1))) AS busy "
        "FROM jobs j WHERE j.worker_id IS NOT NULL AND coalesce(j.dispatched_at, j.started_at) < $2 "
        "AND coalesce(j.finished_at, now()) > $1 AND ($3::text IS NULL OR j.owner = $3) GROUP BY 1", since, until, owner)}
    names = {r["id"]: (r["name"], r["group_id"]) for r in await c.fetch("SELECT id, name, group_id FROM workers")}
    gnames = {r["id"]: r["name"] for r in await c.fetch("SELECT id, name FROM worker_groups")}
    workers = []
    for wid in sorted(set(by_worker) | set(busy), key=lambda w: names.get(w, ("~",))[0]):
        if groups is not None and names.get(wid, (None, None))[1] not in groups:
            continue
        b = _finish(by_worker.get(wid) or _bucket())
        b.update(id=wid, name=names.get(wid, (f"#{wid}",))[0],
                 utilization=round(min(1.0, busy.get(wid, 0.0) / span), 4) if span > 0 else None)
        workers.append(b)
    return {
        "since": since.isoformat(), "until": until.isoformat(), "tz": str(zone),
        "total": _finish(total),
        "groups": [dict(_finish(b), id=g, name=gnames.get(g, "(no group)") if g is not None else "(no group)")
                   for g, b in sorted(by_group.items(), key=lambda x: gnames.get(x[0], "~"))],
        "users": [dict(_finish(b), user=u) for u, b in sorted(by_user.items())] if owner is None else [],
        "workers": workers,
        "daily": [{"date": k, **v} for k, v in daily.items()],
    }
