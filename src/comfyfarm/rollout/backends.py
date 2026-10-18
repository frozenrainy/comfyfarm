"""Restart backends (contract §6, docs/design/07 'Restart backends'). `restart` only delivers the request; whether it
happened is judged by watching the worker (down, then up with the new argv). No silent fallback: a backend that fails
fails the step — an in-app soft restart re-executes the same argv and cannot change versions.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shlex
from urllib.parse import urlparse

import httpx

NAMES = ("manual", "ssh", "http-agent", "systemd-local")
DEFAULT_KEYS_DIR = "/etc/comfyfarm/keys"
TOKEN_ENV_PREFIX = "COMFYFARM_AGENT_TOKEN"        # the only environment variables a token may be read from
SSH_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,31}$")
HOST_NAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:\\-]{0,255}$")


class BackendError(ValueError):
    pass


def in_keys_dir(path, keys_dir: str, what: str) -> str:
    """A secret reference (ssh key, known_hosts, agent token file) must resolve inside the keys directory: otherwise an
    admin could make the router read any file it can read and send it somewhere (a token goes out as a Bearer)."""
    if not isinstance(path, str) or not path or "\0" in path:
        raise BackendError(f"{what} must be a file name in the keys directory {keys_dir}")
    base = os.path.realpath(keys_dir)
    real = os.path.realpath(os.path.join(keys_dir, path))         # relative = relative to the keys directory
    if not real.startswith(base + os.sep):
        raise BackendError(f"{what} must lie in the keys directory {keys_dir}")
    return real


class Backend:
    observe_timeout = 60.0          # the worker must be seen down this soon after `restart`

    def __init__(self, config: dict, keys_dir: str = DEFAULT_KEYS_DIR):
        self.c = config
        self.keys_dir = keys_dir

    async def restart(self, instance: str) -> tuple[bool, str]:
        raise NotImplementedError

    async def status(self, instance: str) -> dict | None:
        """{ActiveState, ExecMainStatus, NRestarts} or None if unknown. ExecMainStatus "2" = the launcher refused."""
        return None

    async def check(self, instance: str) -> list[dict] | None:
        """The host check (docs/design/14) through the agent: [{level, name, detail}], None when this backend has
        no agent to ask. Raises BackendError when the agent could not be asked."""
        return None


class Manual(Backend):
    """The dashboard asks an operator to restart the instance and waits (default 30 minutes) to see it happen."""

    def __init__(self, config, keys_dir=DEFAULT_KEYS_DIR):
        super().__init__(config, keys_dir)
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


def _parse_check(rc: int, text: str) -> list[dict]:
    """The agent's `check` answer: a JSON list of {level, name, detail}; anything else says why it could not run."""
    if rc == 0:
        try:
            out = json.loads(text)
            if isinstance(out, list) and all(isinstance(r, dict) and {"level", "name", "detail"} <= set(r) for r in out):
                return out
        except ValueError:
            pass
    raise BackendError(f"the host check did not run (exit {rc}): {text[-300:] or 'no output'}")


class Ssh(Backend):
    """Per-host key restricted to a forced command (`comfyfarm.agent ssh`): the key can only say
    `restart <instance>` / `status <instance>`. config: host, user, key (a file in the keys directory — the key
    material stays in the file, never in the ledger), port, known_hosts (also in the keys directory). User and host are
    checked and passed so that ssh can never read them as options (`-l user`, `--` before the host)."""

    def __init__(self, config, keys_dir=DEFAULT_KEYS_DIR):
        super().__init__(config, keys_dir)
        for k in ("host", "user", "key"):
            if not self.c.get(k):
                raise BackendError(f"ssh backend needs {k}")
        host, user, port = self.c["host"], self.c["user"], self.c.get("port")
        if not isinstance(user, str) or not SSH_USER.match(user):
            raise BackendError("ssh: user must be a plain account name (letters, digits, . _ -; not starting with -)")
        if not isinstance(host, str) or not (HOST_NAME.match(host) or _is_ip(host)):
            raise BackendError("ssh: host must be a host name or an IP address")
        if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
            raise BackendError("ssh: port must be a number 1..65535")
        self.key = in_keys_dir(self.c["key"], keys_dir, "ssh: key")
        self.known_hosts = in_keys_dir(self.c["known_hosts"], keys_dir, "ssh: known_hosts") if self.c.get("known_hosts") else None

    def argv(self, *cmd: str) -> list[str]:
        a = ["ssh", "-i", self.key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=yes"]
        if self.known_hosts:
            a += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        if self.c.get("port"):
            a += ["-p", str(self.c["port"])]
        return a + ["-l", self.c["user"], "--", self.c["host"], " ".join(shlex.quote(x) for x in cmd)]

    async def restart(self, instance):
        rc, out = await _run(self.argv("restart", instance))
        return rc == 0, out[-500:]

    async def status(self, instance):
        rc, out = await _run(self.argv("status", instance))
        return _parse_status(out) if rc == 0 else None

    async def check(self, instance):
        rc, out = await _run(self.argv("check", instance), timeout=240)
        return _parse_check(rc, out)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class HttpAgent(Backend):
    """Per-host agent (`comfyfarm.agent serve`) with a bearer token. config: url, and the token as a **reference** —
    `token_file` (a file in the keys directory) or `token_env` (an environment variable named COMFYFARM_AGENT_TOKEN…).
    The token value itself is never accepted into the ledger: backups and replicas must not carry it (same reasoning
    as D-29). Only such references are accepted: the token is sent to `url`, so any other file or variable would be."""

    def __init__(self, config, keys_dir=DEFAULT_KEYS_DIR):
        super().__init__(config, keys_dir)
        if "token" in self.c:
            raise BackendError("http-agent: give token_file or token_env, never the token value (it would be stored)")
        if not self.c.get("url") or not (self.c.get("token_file") or self.c.get("token_env")):
            raise BackendError("http-agent backend needs url and token_file or token_env")
        u = urlparse(str(self.c["url"]))
        if u.scheme not in ("http", "https") or not u.hostname:
            raise BackendError("http-agent: url must be http(s)://host[:port]")
        self.token_file = in_keys_dir(self.c["token_file"], keys_dir, "http-agent: token_file") if self.c.get("token_file") else None
        env = self.c.get("token_env")
        if not self.token_file and not (isinstance(env, str) and env.startswith(TOKEN_ENV_PREFIX) and re.fullmatch(r"[A-Z0-9_]+", env)):
            raise BackendError(f"http-agent: token_env must be a variable named {TOKEN_ENV_PREFIX}…")

    def token(self) -> str:
        if self.token_file:
            try:
                with open(self.token_file) as f:
                    return f.read().strip()
            except OSError as e:
                raise BackendError(f"http-agent: cannot read token_file: {type(e).__name__}")
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

    async def check(self, instance):
        try:
            r = await self._call("GET", "check", instance)
        except httpx.HTTPError as e:
            raise BackendError(f"the agent did not answer: {type(e).__name__}")
        return _parse_check(0 if r.status_code == 200 else r.status_code, r.text)


class SystemdLocal(Backend):
    """Router and worker on the same host. config: unit (default comfyfarm@{instance}), user (bool)."""

    def __init__(self, config, keys_dir=DEFAULT_KEYS_DIR):
        super().__init__(config, keys_dir)
        if not UNIT.match(str(self.c.get("unit", "comfyfarm@{instance}")).replace("{instance}", "x")):
            raise BackendError("systemd-local: unit must be a unit name (may contain {instance})")

    def _cmd(self, verb: str, instance: str, *opts: str) -> list[str]:
        unit = self.c.get("unit", "comfyfarm@{instance}").replace("{instance}", instance)
        return ["systemctl"] + (["--user"] if self.c.get("user") else []) + [verb, *opts, "--", unit]

    async def restart(self, instance):
        rc, out = await _run(self._cmd("restart", instance))
        return rc == 0, out[-500:]

    async def status(self, instance):
        rc, out = await _run(self._cmd("show", instance, "-p", "ActiveState,ExecMainStatus,NRestarts"))
        return _parse_status(out) if rc == 0 else None


REGISTRY: dict[str, type[Backend]] = {"manual": Manual, "ssh": Ssh, "http-agent": HttpAgent, "systemd-local": SystemdLocal}


def make(name: str, config: dict, keys_dir: str | None = None) -> Backend:
    """Validates the configuration (the API calls this before storing it, the engine before every step)."""
    if name not in REGISTRY:
        raise BackendError(f"unknown restart backend {name!r} (known: {', '.join(REGISTRY)})")
    if not isinstance(config or {}, dict):
        raise BackendError("restart_config must be an object")
    return REGISTRY[name](config or {}, keys_dir=keys_dir or DEFAULT_KEYS_DIR)
