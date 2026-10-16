import asyncio, pathlib, socket, uuid

import asyncpg
import pytest
import pytest_asyncio
from aiohttp import web

from comfyfarm.app import Router
from comfyfarm.core.settings import Settings
from comfyfarm.testing.fake_worker import FakeWorker

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def pg_uri():
    import pgserver
    srv = pgserver.get_server(ROOT / "lab" / "pgdata-test", cleanup_mode="stop")
    return srv.get_uri()


@pytest_asyncio.fixture
async def dsn(pg_uri):
    """A fresh database per test — never share one between two routers or tests (docs/design/11)."""
    name = "t_" + uuid.uuid4().hex[:12]
    admin = await asyncpg.connect(pg_uri)
    await admin.execute(f"CREATE DATABASE {name}")
    await admin.close()
    yield pg_uri.replace("/postgres?", f"/{name}?")
    admin = await asyncpg.connect(pg_uri)
    await admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
    await admin.close()


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class FakeHandle:
    def __init__(self, fake, runner, port):
        self.fake, self.runner, self.port = fake, runner, port
        self.url = f"http://127.0.0.1:{port}"

    async def stop(self):
        await self.runner.cleanup()


@pytest_asyncio.fixture
async def fakes():
    started = []

    async def start(**kw):
        fake = FakeWorker(**kw)
        runner = web.AppRunner(fake.app)
        await runner.setup()
        port = free_port()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        h = FakeHandle(fake, runner, port)
        started.append(h)
        return h

    yield start
    for h in started:
        try:
            await h.stop()
        except Exception:
            pass


def fast_settings(dsn, **kw):
    base = dict(dispatch_period=0.2, health_period=0.3, reconcile_period=0.3, dead_after=1.0, busy_after=3.0,
                lost_grace=2.0, absent_settle=1.0, worker_lost_after=2.0, dev_identity="tester")
    base.update(kw)
    return Settings(dsn=dsn, **base)


@pytest_asyncio.fixture
async def router_factory(dsn):
    routers = []

    async def make(**kw):
        r = Router(fast_settings(dsn, **kw))
        await r.start()
        routers.append(r)
        return r

    yield make
    for r in routers:
        await r.stop()


async def add_worker(router, url, group="g1", name=None):
    async with router.db.conn() as c:
        gid = await c.fetchval("INSERT INTO worker_groups(name) VALUES ($1) ON CONFLICT (name) DO UPDATE SET name = excluded.name RETURNING id", group)
        wid = await c.fetchval("INSERT INTO workers(name, base_url, group_id) VALUES ($1, $2, $3) RETURNING id",
                               name or url, url, gid)
    return gid, wid


async def wait_status(router, prompt_id, statuses, timeout=20.0):
    statuses = (statuses,) if isinstance(statuses, str) else statuses
    t = asyncio.get_running_loop().time()
    last = None
    while asyncio.get_running_loop().time() - t < timeout:
        async with router.db.conn() as c:
            last = await c.fetchrow("SELECT status, error, worker_id FROM jobs WHERE prompt_id = $1", uuid.UUID(prompt_id))
        if last and last["status"] in statuses:
            return dict(last)
        await asyncio.sleep(0.1)
    raise AssertionError(f"job {prompt_id} did not reach {statuses}; last={dict(last) if last else None}")


def tiny(seed=0, blur=1, size=64):
    return {"1": {"class_type": "EmptyImage", "inputs": {"width": size, "height": size, "batch_size": 1, "color": seed}},
            "2": {"class_type": "ImageBlur", "inputs": {"image": ["1", 0], "blur_radius": blur, "sigma": 1.0}},
            "3": {"class_type": "PreviewImage", "inputs": {"images": ["2", 0]}}}
