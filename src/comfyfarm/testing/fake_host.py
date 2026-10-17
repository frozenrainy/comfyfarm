"""A worker machine for rollout tests: a FakeWorker on a fixed port, restarted the way the launcher does it — go down,
fetch this instance's render from the router, validate it with the launcher's own rules (READY files included), come
back with the render's argv. Plus the ways real restarts go wrong:

  normal      as above
  noop        the restart request is accepted and nothing happens (a dead agent, a wrong unit name)
  never_up    goes down and stays down (crash at import)
  wrong_argv  comes back, but still with the old argv (the service manager ran something else)
  broken      comes back with the new argv, but /object_info fails (a custom node broke)

`status` mimics systemd: ExecMainStatus "2" when the launcher refused the render.
"""
from __future__ import annotations

import asyncio

import httpx
from aiohttp import web

from .. import launcher
from ..rollout.backends import Backend
from .fake_worker import FakeWorker


class FakeHost:
    def __init__(self, port: int, *, router: httpx.AsyncClient, instance: str, art: str, local: str, prefixes: list[str],
                 backend: str = "cuda", down_seconds: float = 1.0):
        self.port, self.router, self.instance = port, router, instance
        self.art, self.local, self.prefixes = art, local, prefixes
        self.backend, self.down_seconds = backend, down_seconds
        self.mode = "normal"            # every restart…
        self.modes: list[str] = []      # …unless one is queued here (consumed one per restart)
        self.argv = ["main.py", "--listen", "0.0.0.0"]
        self.version = "0.41.0-fake"
        self.status = {"ActiveState": "active", "ExecMainStatus": "0", "NRestarts": 0}
        self.restarts = 0
        self.worker: FakeWorker | None = None
        self.runner: web.AppRunner | None = None
        self.task: asyncio.Task | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self, object_info_ok: bool = True):
        self.worker = FakeWorker(ws_fix=True, backend=self.backend, version=self.version)
        self.worker.argv = list(self.argv)
        self.worker.object_info_ok = object_info_ok
        self.runner = web.AppRunner(self.worker.app, shutdown_timeout=0.5)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()

    async def stop(self):
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    def request_restart(self):
        self.restarts += 1
        self.task = asyncio.create_task(self._restart())

    async def _restart(self):
        mode = self.modes.pop(0) if self.modes else self.mode
        if mode == "noop":
            return
        await self.stop()
        self.status = {**self.status, "ActiveState": "activating"}
        await asyncio.sleep(self.down_seconds)
        if mode == "never_up":
            self.status = {**self.status, "ActiveState": "failed", "ExecMainStatus": "1"}
            return
        r = await self.router.get("/api/worker_profile", params={"instance": self.instance})
        try:
            if r.status_code != 200:
                raise launcher.Refused(f"router answered {r.status_code}")
            render = r.json()
            launcher.validate(render, instance=self.instance, art=self.art, local=self.local, prefixes=self.prefixes)
        except launcher.Refused:
            self.status = {**self.status, "ActiveState": "failed", "ExecMainStatus": "2"}
            return
        if mode != "wrong_argv":
            self.argv = render["argv"][1:]
            self.version = render["argv"][1].split("/core/")[1].split("/")[0] + "-fake"   # "<core name>-fake"
        await self.start(object_info_ok=mode != "broken")
        self.status = {"ActiveState": "active", "ExecMainStatus": "0", "NRestarts": self.status["NRestarts"] + 1}


class FakeHostBackend(Backend):
    observe_timeout = 10.0

    def __init__(self, hosts: dict[str, FakeHost]):
        super().__init__({})
        self.hosts = hosts

    async def restart(self, instance):
        self.hosts[instance].request_restart()
        return True, "restart requested"

    async def status(self, instance):
        return dict(self.hosts[instance].status)
