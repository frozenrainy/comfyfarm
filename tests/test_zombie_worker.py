"""A worker that answers and runs nothing (field, dev.21): a GPU fault killed ComfyUI's prompt executor thread, the
process stayed up and /queue kept answering 200 — the router kept it online and the next job waited forever.

The router now asks more than /queue: the extension's GET /comfyfarm/health from 0.9, /system_stats of an older one
(500 with a broken GPU context). A broken worker goes offline like a dead one; from 0.9 the extension's watchdog
exits the process and the service manager starts it again; a job that waited in its queue and never ran goes back in
line (D-43); one that had started stays lost (D-06: test_core_scenarios, the amnesia test)."""
import asyncio
import importlib.util
import pathlib
import types
import uuid

from comfyfarm.core import jobs as jobsvc
from conftest import add_worker, tiny, wait_status

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("watchdog", ROOT / "worker_ext" / "comfyfarm_worker" / "watchdog.py")
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


async def submit(router, seed):
    job, _ = await jobsvc.submit(router.db, owner="tester", prompt=tiny(seed))
    router.dispatcher.wake.set()
    return str(job["prompt_id"])


async def worker(router, wid):
    async with router.db.conn() as c:
        return dict(await c.fetchrow("SELECT state, failure_kind, capabilities FROM workers WHERE id = $1", wid))


async def until(fn, timeout=20.0):
    t = asyncio.get_running_loop().time()
    while not await fn():
        assert asyncio.get_running_loop().time() - t < timeout, "timed out"
        await asyncio.sleep(0.1)


async def reasons(router, pid):
    async with router.db.conn() as c:
        return [(x["to_status"], x["reason"]) for x in await c.fetch(
            "SELECT to_status, reason FROM job_events WHERE job_id = (SELECT id FROM jobs WHERE prompt_id=$1) ORDER BY id",
            uuid.UUID(pid))]


async def test_a_dead_executor_takes_the_worker_out_and_the_waiting_job_comes_back(router_factory, fakes):
    w = await fakes(secured=False, extension="0.9")
    r = await router_factory()
    _, wid = await add_worker(r, w.url)
    first = await submit(r, 1)
    await wait_status(r, first, "completed")
    assert (await worker(r, wid))["state"] == "online"

    w.fake.kill_executor()                               # the process answers on, nothing runs
    stuck = await submit(r, 2)
    await wait_status(r, stuck, "running")               # accepted into the worker's queue, never started

    async def offline():
        return (await worker(r, wid))["state"] == "offline"
    await until(offline)
    row = await worker(r, wid)
    assert row["failure_kind"] == "broken" and row["capabilities"]["broken_as"] == "its prompt executor has stopped"
    later = await submit(r, 3)
    await asyncio.sleep(1.0)
    assert w.fake.post_log.count(later) == 0              # nothing more is sent to it

    w.fake.restart()                                     # what the watchdog's exit and the service manager do
    await wait_status(r, stuck, "completed", timeout=30)  # back in line (D-43), then run on the fresh process
    await wait_status(r, later, "completed", timeout=30)
    assert ("pending", "the worker restarted before running it") in await reasons(r, stuck)
    assert (await worker(r, wid))["state"] == "online"


async def test_an_older_worker_with_a_broken_gpu_is_found_by_system_stats(router_factory, fakes):
    w = await fakes(secured=False, extension="0.8")      # before 0.9: no /comfyfarm/health
    r = await router_factory()
    _, wid = await add_worker(r, w.url)
    await wait_status(r, await submit(r, 1), "completed")
    w.fake.gpu_fault = "CUDA error: an illegal memory access was encountered"

    async def broken():
        row = await worker(r, wid)
        return row["state"] == "offline" and row["failure_kind"] == "broken"
    await until(broken)
    assert "HTTP 500" in (await worker(r, wid))["capabilities"]["broken_as"]
    w.fake.gpu_fault = None                              # restarted by hand: online again
    async def online():
        return (await worker(r, wid))["state"] == "online"
    await until(online)


def test_the_watchdog_exits_once_the_executor_it_saw_is_gone():
    T = lambda name, alive=True: types.SimpleNamespace(name=name, is_alive=lambda: alive)
    w = watchdog.Watch()
    assert w.check([T("MainThread")]) == "unknown"                     # not started yet: nothing to say
    assert w.check([T("MainThread"), T("Thread-1 (prompt_worker)")]) == "alive"
    assert w.check([T("MainThread"), T("Thread-1 (prompt_worker)", alive=False)]) == "dead"
    assert w.check([T("MainThread")]) == "dead"                        # gone from the list: dead too, never unknown again

    exits, threads = [], [[T("Thread-1 (prompt_worker)")], [T("Thread-1 (prompt_worker)")], [T("MainThread")]]
    watch = watchdog.Watch()
    watch_check = watch.check
    watch.check = lambda: watch_check(threads.pop(0))
    watchdog.run(exit=exits.append, sleep=lambda s: None, clock=lambda: 0.0, watch=watch)
    assert exits == [watchdog.EXIT_CODE] and watchdog.EXIT_CODE != 2  # 2 is the launcher's "do not restart"

    gave_up = watchdog.Watch()
    gave_up.check = lambda: "unknown"
    clock = iter([0.0, 10.0, watchdog.FIND_FOR + 1])
    watchdog.run(exit=exits.append, sleep=lambda s: None, clock=lambda: next(clock), watch=gave_up)
    assert exits == [watchdog.EXIT_CODE]                               # a ComfyUI without that thread is left alone
    assert watchdog.start({"COMFYFARM_EXECUTOR_WATCHDOG": "0"}) is False


def test_the_watchdog_exits_when_the_gpu_context_is_lost():
    """0.10: an illegal address is sticky — the executor may live on, but every job in that process would fail. Two
    broken looks in a row, not one (a call can race a reset)."""
    class Done(Exception):
        pass

    def run_with(looks):
        exits, seen = [], iter(looks)
        watch = watchdog.Watch()
        check = watch.check
        watch.check = lambda: check([types.SimpleNamespace(name="Thread-1 (prompt_worker)", is_alive=lambda: True)])

        def device():
            try:
                return next(seen)
            except StopIteration:
                raise Done
        try:
            watchdog.run(exit=exits.append, sleep=lambda s: None, clock=lambda: 0.0, watch=watch, device=device)
        except Done:
            pass
        return exits
    assert run_with(["ok", "error: AcceleratorError: x", "ok", "error: x", "error: x"]) == [watchdog.EXIT_CODE]
    assert run_with(["ok", "error: x", "ok", "error: x", "ok", "cpu"]) == []


async def test_a_job_failed_by_a_gpu_fault_runs_again_once(router_factory, fakes):
    """D-44: the GPU's fault, not the workflow's — a new job at the front, once per chain; any other error, never."""
    w = await fakes()
    r = await router_factory()
    await add_worker(r, w.url)
    fault = "CUDA error: an illegal memory access was encountered\nSearch for `hipErrorIllegalAddress'"

    async def child_of(pid):
        async with r.db.conn() as c:
            return await c.fetchval("SELECT k.prompt_id::text FROM jobs k JOIN jobs j ON k.retry_of = j.id "
                                    "WHERE j.prompt_id = $1", uuid.UUID(pid))

    async def retried(pid):
        async def check():
            return await child_of(pid) is not None
        await until(check)
        return await child_of(pid)

    w.fake.mode, w.fake.error_message = "error", fault
    first = await submit(r, 1)
    await wait_status(r, first, "failed")
    second = await retried(first)
    assert any(why.startswith("automatic retry of job") for _, why in await reasons(r, second))
    await wait_status(r, second, "failed")               # the same fault again: the chain stops here
    await asyncio.sleep(1.5)
    assert await child_of(second) is None

    w.fake.error_message = "fake execution error"        # not a GPU fault: a person's call
    third = await submit(r, 3)
    await wait_status(r, third, "failed")
    await asyncio.sleep(1.5)
    assert await child_of(third) is None

    w.fake.error_message = fault                         # a fault, then the retry runs clean
    fourth = await submit(r, 4)
    await wait_status(r, fourth, "failed")
    w.fake.mode = "normal"
    await wait_status(r, await retried(fourth), "completed")


async def test_no_automatic_retry_when_turned_off(router_factory, fakes):
    w = await fakes()
    r = await router_factory(retry_gpu_faults=0)
    await add_worker(r, w.url)
    w.fake.mode, w.fake.error_message = "error", "CUDA error: an illegal memory access was encountered"
    pid = await submit(r, 1)
    await wait_status(r, pid, "failed")
    await asyncio.sleep(1.5)
    async with r.db.conn() as c:
        assert await c.fetchval("SELECT count(*) FROM jobs WHERE retry_of IS NOT NULL") == 0


async def test_a_worker_with_an_older_extension_is_told_to_build(dsn, fakes):
    """The router's upgrade does not reach a worker until a Build and a rollout (field, dev.22)."""
    import httpx
    from comfyfarm.api.admin import bundled_extension
    from comfyfarm.app import create_app
    from conftest import fast_settings
    old, new = await fakes(secured=True, extension="0.8"), await fakes(secured=True, extension=bundled_extension())
    app = create_app(fast_settings(dsn))
    async with app.router.lifespan_context(app):
        r = app.state.router
        await add_worker(r, old.url, name="old")
        await add_worker(r, new.url, name="new")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://farm") as c:
            admin = {"X-ComfyFarm-User": "admin"}

            async def probed():
                ws = (await c.get("/api/admin/workers", headers=admin)).json()
                return all((w["capabilities"] or {}).get("extension") for w in ws)
            await until(probed)
            ws = {w["name"]: w["warnings"] for w in (await c.get("/api/admin/workers", headers=admin)).json()}
    want = f"worker extension 0.8 is older than this router's {bundled_extension()}: Build, then roll out a profile made from the new build"
    assert want in ws["old"]
    assert not any("is older than this router" in x for x in ws["new"])
