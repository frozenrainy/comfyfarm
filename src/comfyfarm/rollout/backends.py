"""Restart backends (contract §6, docs/design/07 'Restart backends'). `restart` only delivers the request; whether it
happened is judged by watching the worker (down, then up with the new argv). No silent fallback: a backend that fails
fails the step — an in-app soft restart re-executes the same argv and cannot change versions.
"""
from __future__ import annotations

import asyncio
import json
import shlex

import httpx

NAMES = ("manual", "ssh", "http-agent", "systemd-local")


class BackendError(ValueError):
    pass


class Backend:
    observe_timeout = 60.0          # the worker must be seen down this soon after `restart`

    def __init__(self, config: dict):
        self.c = config

    async def restart(self, instance: str) -> tuple[bool, str]:
        raise NotImplementedError

    async def status(self, instance: str) -> dict | None:
        """{ActiveState, ExecMainStatus, NRestarts} or None if unknown. ExecMainStatus "2" = the launcher refused."""
        return None


class Manual(Backend):
    """The dashboard asks an operator to restart the instance and waits (default 30 minutes) to see it happen."""

    def __init__(self, config):
        super().__init__(config)
        self.observe_timeout = float(config.get("timeout", 1800))

    async def restart(self, instance):
        return True, f"waiting for an operator to restart {instance}"


async def _run(argv: list[str], timeout: float = 30.0) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except TimeoutError:
        p.kill()
        return 124, "timed out"
    return p.returncode, out.decode(errors="replace").strip()


def _parse_status(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except ValueError:
            return None
    d = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)      # systemctl show
    return {k: d[k] for k in ("ActiveState", "ExecMainStatus", "NRestarts") if k in d} or None


class Ssh(Backend):
    """Per-host key restricted to a forced command (`comfyfarm.agent ssh`): the key can only say
    `restart <instance>` / `status <instance>`. config: host, user, key (a path on the router — the key material
    stays in the file, never in the ledger), port, known_hosts."""

    def argv(self, *cmd: str) -> list[str]:
        for k in ("host", "user", "key"):
            if not self.c.get(k):
                raise BackendError(f"ssh backend needs {k}")
        a = ["ssh", "-i", self.c["key"], "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=yes"]
        if self.c.get("known_hosts"):
            a += ["-o", f"UserKnownHostsFile={self.c['known_hosts']}"]
        if self.c.get("port"):
            a += ["-p", str(self.c["port"])]
        return a + [f"{self.c['user']}@{self.c['host']}", " ".join(shlex.quote(x) for x in cmd)]

    async def restart(self, instance):
        rc, out = await _run(self.argv("restart", instance))
        return rc == 0, out[-500:]

    async def status(self, instance):
        rc, out = await _run(self.argv("status", instance))
        return _parse_status(out) if rc == 0 else None


class HttpAgent(Backend):
    """Per-host agent (`comfyfarm.agent serve`) with a bearer token. config: url, and the token as a **reference** —
    `token_file` (path readable by the router) or `token_env` (environment variable name). The token value itself is
    never accepted into the ledger: backups and replicas must not carry it (same reasoning as D-29)."""

    def __init__(self, config):
        super().__init__(config)
        if "token" in self.c:
            raise BackendError("http-agent: give token_file or token_env, never the token value (it would be stored)")
        if not self.c.get("url") or not (self.c.get("token_file") or self.c.get("token_env")):
            raise BackendError("http-agent backend needs url and token_file or token_env")

    def token(self) -> str:
        if self.c.get("token_file"):
            try:
                with open(self.c["token_file"]) as f:
                    return f.read().strip()
            except OSError as e:
                raise BackendError(f"http-agent: cannot read token_file: {type(e).__name__}")
        import os
        value = os.environ.get(self.c["token_env"], "")
        if not value:
            raise BackendError(f"http-agent: environment variable {self.c['token_env']} is empty on the router")
        return value

    async def _call(self, method: str, path: str, instance: str):
        token = self.token()
        async with httpx.AsyncClient(timeout=30) as h:
            return await h.request(method, f"{self.c['url'].rstrip('/')}/{path}", params={"instance": instance},
                                   headers={"Authorization": f"Bearer {token}"})

    async def restart(self, instance):
        try:
            r = await self._call("POST", "restart", instance)
        except httpx.HTTPError as e:
            return False, f"agent unreachable: {type(e).__name__}"
        return r.status_code == 200, r.text[-500:]

    async def status(self, instance):
        try:
            r = await self._call("GET", "status", instance)
            return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError, BackendError):
            return None


class SystemdLocal(Backend):
    """Router and worker on the same host. config: unit (default comfyfarm@{instance}), user (bool)."""

    def _cmd(self, verb: str, instance: str) -> list[str]:
        unit = self.c.get("unit", "comfyfarm@{instance}").format(instance=instance)
        return ["systemctl"] + (["--user"] if self.c.get("user") else []) + [verb, unit]

    async def restart(self, instance):
        rc, out = await _run(self._cmd("restart", instance))
        return rc == 0, out[-500:]

    async def status(self, instance):
        rc, out = await _run(self._cmd("show", instance) + ["-p", "ActiveState,ExecMainStatus,NRestarts"])
        return _parse_status(out) if rc == 0 else None


REGISTRY: dict[str, type[Backend]] = {"manual": Manual, "ssh": Ssh, "http-agent": HttpAgent, "systemd-local": SystemdLocal}


def make(name: str, config: dict) -> Backend:
    if name not in REGISTRY:
        raise BackendError(f"unknown restart backend {name!r} (known: {', '.join(REGISTRY)})")
    return REGISTRY[name](config or {})
