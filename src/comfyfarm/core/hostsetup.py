"""Router side of worker setup (docs/design/14): the agent key, the setup script, pinned host keys, and running the
script over ssh with sudo when the operator chose that (D-41)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shlex
import stat
import tempfile
from importlib import resources

AGENT_KEY = "comfyfarm-agent"                 # in the keys directory; the public half goes into every worker setup
KNOWN_HOSTS = "known_hosts"
TOOLS = ("launcher.py", "agent.py", "hostcheck.py")
PAYLOAD_LINE = re.compile(r"^PAYLOAD = None.*$", re.M)
HOST_KEY = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|ssh-rsa) [A-Za-z0-9+/=]+$")


class SetupError(Exception):
    pass


async def _run(argv: list[str], *, stdin: bytes | None = None, env: dict | None = None, timeout: float = 60) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
    try:
        out, _ = await asyncio.wait_for(p.communicate(stdin), timeout)
    except TimeoutError:
        p.kill()
        return 124, "timed out"
    return p.returncode, out.decode(errors="replace")


async def agent_public_key(keys_dir: str) -> str:
    """The router's agent key pair, created on first use. Only the public half ever leaves the keys directory."""
    path = os.path.join(keys_dir, AGENT_KEY)
    if not os.path.exists(path):
        if not os.access(keys_dir, os.W_OK):
            raise SetupError(f"the keys directory {keys_dir} is not writable by the router: create the agent key there "
                             f"once with  ssh-keygen -t ed25519 -N '' -C comfyfarm-router -f {path}")
        rc, out = await _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "comfyfarm-router", "-f", path])
        if rc != 0:
            raise SetupError(f"ssh-keygen failed: {out.strip()[-300:]}")
    with open(path + ".pub") as f:
        return f.read().strip()


def tool_files() -> dict[str, str]:
    pkg = resources.files("comfyfarm")
    return {name: base64.b64encode(pkg.joinpath(name).read_bytes()).decode() for name in TOOLS}


SHARE = ("recipes", "worker_ext/comfyfarm_worker", "ui_ext/comfyfarm_ui")


def share_dir() -> str:
    """recipes/, worker_ext/, ui_ext/ — beside the package in a checkout, /opt/comfyfarm/share in the image."""
    for d in (os.environ.get("COMFYFARM_SHARE_DIR"), "/opt/comfyfarm/share",
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")):
        if d and all(os.path.isdir(os.path.join(d, s)) for s in SHARE):
            return os.path.abspath(d)
    raise SetupError("the router has no recipes and extensions to send (COMFYFARM_SHARE_DIR) — builds from the "
                     "dashboard cannot be set up with this installation")


def build_files() -> dict[str, str]:
    """What a host that allows builds from the dashboard gets besides the worker tools (worker_setup.builds)."""
    pkg = resources.files("comfyfarm")
    out = {name: base64.b64encode(pkg.joinpath(name).read_bytes()).decode() for name in ("publish.py", "build.py")}
    root = share_dir()
    for sub in SHARE:
        for d, dirs, names in os.walk(os.path.join(root, sub)):
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x != "__pycache__")
            for n in sorted(names):
                if n.startswith(".") or n.endswith(".pyc"):
                    continue
                full = os.path.join(d, n)
                with open(full, "rb") as f:
                    out[os.path.relpath(full, root)] = base64.b64encode(f.read()).decode()
    return out


def build_script(spec: dict, report_url: str, version: str) -> tuple[str, str]:
    """The setup script with its parameters and the worker tools of this router version. Returns (text, sha256)."""
    src = resources.files("comfyfarm").joinpath("worker_setup.py").read_text()
    payload = {"spec": spec, "files": tool_files(), "report_url": report_url, "version": version}
    if spec.get("builds"):
        payload["build_files"] = build_files()
    blob = base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    text, n = PAYLOAD_LINE.subn(f'PAYLOAD = json.loads(base64.b64decode("{blob}").decode())', src, count=1)
    if n != 1:
        raise SetupError("worker_setup.py has no PAYLOAD line")
    return text, hashlib.sha256(text.encode()).hexdigest()


def operator_command(url: str, sha256: str) -> str:
    """What the operator runs on the host: python3 is there anyway (the launcher needs it); curl may not be. The
    download is checked against the checksum the dashboard shows before anything runs as root."""
    fetch = ('import sys,hashlib,urllib.request;d=urllib.request.urlopen(sys.argv[1]).read();'
             'assert hashlib.sha256(d).hexdigest()==sys.argv[2],"checksum mismatch: not the script the dashboard made";'
             'open("/tmp/comfyfarm-setup.py","wb").write(d)')
    return f"python3 -c {shlex.quote(fetch)} {shlex.quote(url)} {sha256} && sudo python3 /tmp/comfyfarm-setup.py"


def pin_host_keys(keys_dir: str, host: str, keys: list[str], port: int = 22) -> int:
    """Writes the host's keys (reported by the setup script itself) into known_hosts, replacing earlier lines for that
    host: later ssh connections check against them — no trust on first use."""
    good = [k for k in keys if HOST_KEY.match(k)]
    if not good:
        raise SetupError("the host reported no usable ssh host key")
    name = host if port == 22 else f"[{host}]:{port}"
    path = os.path.join(keys_dir, KNOWN_HOSTS)
    try:
        with open(path) as f:
            lines = [line for line in f.read().splitlines() if line.split(" ", 1)[0] != name]
    except FileNotFoundError:
        lines = []
    lines += [f"{name} {k}" for k in good]
    tmp = path + ".new"
    with open(tmp, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return len(good)


async def run_over_ssh(host: str, port: int, user: str, password: str, script: str, log) -> int:
    """D-41, second way: log in with the operator's account, put the script into a private temporary file, run it with
    sudo. The password reaches ssh through SSH_ASKPASS (an environment variable of the ssh process only) and sudo
    through stdin; it is never written anywhere. `log(text)` receives the output as it comes.

    The host key is not known yet: it is accepted for this session only (a throw-away known_hosts) and the keys the
    script reports are what gets pinned."""
    with tempfile.TemporaryDirectory(prefix="comfyfarm-ssh-") as d:
        askpass = os.path.join(d, "askpass")
        with open(askpass, "w") as f:
            f.write('#!/bin/sh\nprintf "%s\\n" "$COMFYFARM_SETUP_PASSWORD"\n')
        os.chmod(askpass, stat.S_IRWXU)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "SSH_ASKPASS": askpass, "SSH_ASKPASS_REQUIRE": "force",
               "DISPLAY": "comfyfarm:0", "COMFYFARM_SETUP_PASSWORD": password}
        base = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={d}/known_hosts",
                "-o", "PreferredAuthentications=password,keyboard-interactive", "-o", "PubkeyAuthentication=no",
                "-o", "NumberOfPasswordPrompts=1", "-o", "ConnectTimeout=15", "-p", str(port), "-l", user, "--", host]
        rc, out = await _run(base + ['f=$(mktemp /tmp/comfyfarm-setup.XXXXXX) && chmod 600 "$f" && cat > "$f" && echo "$f"'],
                             stdin=script.encode(), env=env, timeout=60)
        path = out.strip().splitlines()[-1] if rc == 0 and out.strip() else ""
        if rc != 0 or not re.fullmatch(r"/tmp/comfyfarm-setup\.[A-Za-z0-9]+", path):
            log(f"could not log in to {user}@{host}:{port}: {out.strip()[-300:] or rc}\n")
            return rc or 1
        log(f"logged in as {user}@{host}; running the setup script with sudo\n")
        cmd = (f"sudo -S -p '' python3 {path}; rc=$?; rm -f {path}; exit $rc" if user != "root"
               else f"python3 {path}; rc=$?; rm -f {path}; exit $rc")
        p = await asyncio.create_subprocess_exec(*base, cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT, env=env)
        p.stdin.write(password.encode() + b"\n")
        await p.stdin.drain()
        p.stdin.close()
        try:
            async with asyncio.timeout(1800):
                async for line in p.stdout:
                    log(line.decode(errors="replace"))
                return await p.wait()
        except TimeoutError:
            p.kill()
            log("timed out after 30 minutes\n")
            return 124
