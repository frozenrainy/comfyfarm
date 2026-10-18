"""Restart agent for a worker host (contract §6): the only things the router may ask a host are `restart <instance>`,
`status <instance>` and `check <instance>` (the read-only host check, docs/design/14). Standard library only —
installed by worker setup into /opt/comfyfarm/worker/ (root-owned: the router cannot replace it, D-40).

SSH forced command (backend `ssh`): in ~/.ssh/authorized_keys of the service account

    command="python3 /opt/comfyfarm/agent.py ssh",no-port-forwarding,no-pty,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA…

HTTP (backend `http-agent`, e.g. Windows hosts later):  python3 agent.py serve --port 8399 --token-file /etc/comfyfarm/token

Configuration (environment, else /etc/comfyfarm/agent.env — a forced command cannot pass environment):
  COMFYFARM_AGENT_INSTANCES   comma-separated instances this host serves (anything else is refused)
  COMFYFARM_AGENT_UNIT        systemd unit template, default comfyfarm@{instance}
  COMFYFARM_AGENT_USER        1 = user units (systemctl --user; enable lingering for the account)
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

INSTANCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")


class Refused(Exception):
    pass


CONFIG = os.environ.get("COMFYFARM_AGENT_CONFIG", "/etc/comfyfarm/agent.env")
HERE = os.path.dirname(os.path.abspath(__file__))


def setting(key: str, default: str = "") -> str:
    if key in os.environ:
        return os.environ[key]
    try:
        with open(CONFIG) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k.strip() == key:
                    return v.strip()
    except OSError:
        pass
    return default


def instances() -> set[str]:
    return {x.strip() for x in setting("COMFYFARM_AGENT_INSTANCES").split(",") if x.strip()}


def served(instance: str) -> str:
    if not INSTANCE.match(instance) or instance not in instances():
        raise Refused(f"instance {instance!r} is not served here")
    return instance


def unit(instance: str) -> str:
    return setting("COMFYFARM_AGENT_UNIT", "comfyfarm@{instance}").format(instance=served(instance))


def systemctl(*args: str) -> subprocess.CompletedProcess:
    base = ["systemctl"] + (["--user"] if setting("COMFYFARM_AGENT_USER") == "1" else [])
    return subprocess.run(base + list(args), capture_output=True, text=True, timeout=60)


def do(verb: str, instance: str) -> tuple[int, str]:
    """(exit code, output). restart: delivery only — the router watches the worker go down and come back."""
    u = unit(instance)
    if verb == "restart":
        p = systemctl("restart", "--no-block", u)
        return p.returncode, (p.stdout + p.stderr).strip() or "restart requested"
    if verb == "status":
        p = systemctl("show", u, "-p", "ActiveState,ExecMainStatus,NRestarts")
        d = dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)
        return p.returncode, json.dumps({k: d.get(k) for k in ("ActiveState", "ExecMainStatus", "NRestarts")})
    if verb == "check":                         # read-only; exit 0 = the check ran (its findings are in the JSON)
        p = subprocess.run([sys.executable, os.path.join(HERE, "hostcheck.py"), served(instance), "--json"],
                           capture_output=True, text=True, timeout=180)
        if p.returncode not in (0, 1):
            return p.returncode, (p.stderr or p.stdout).strip()
        return 0, p.stdout.strip()
    raise Refused(f"unknown command {verb!r}")


def ssh_main() -> int:
    words = os.environ.get("SSH_ORIGINAL_COMMAND", "").split()
    try:
        if len(words) != 2:
            raise Refused("expected: restart|status|check <instance>")
        rc, out = do(words[0], words[1])
    except Refused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(out)
    return rc


def serve(port: int, token: str, host: str = "0.0.0.0"):
    class H(BaseHTTPRequestHandler):
        def _go(self, verb: str):
            # bytes: compare_digest raises on non-ASCII str, and the header is whatever the client sent
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode("utf-8", "surrogateescape"),
                                       f"Bearer {token}".encode()):
                return self._send(401, "unauthorized")
            q = parse_qs(urlparse(self.path).query)
            try:
                rc, out = do(verb, (q.get("instance") or [""])[0])
            except Refused as e:
                return self._send(403, str(e))
            self._send(200 if rc == 0 else 500, out)

        def _send(self, code: int, body: str):
            self.send_response(code)
            self.send_header("Content-Type", "application/json" if body.startswith("{") else "text/plain")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_POST(self):
            if urlparse(self.path).path == "/restart":
                return self._go("restart")
            self._send(404, "not found")

        def do_GET(self):
            if urlparse(self.path).path in ("/status", "/check"):
                return self._go(urlparse(self.path).path[1:])
            self._send(404, "not found")

        def log_message(self, *a):
            pass
    ThreadingHTTPServer((host, port), H).serve_forever()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="comfyfarm-agent")
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("ssh")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8399)
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--token-file", required=True)
    a = ap.parse_args(argv)
    if a.mode == "ssh":
        return ssh_main()
    with open(a.token_file) as f:
        token = f.read().strip()
    if len(token) < 16:
        ap.error("token too short (at least 16 characters)")
    serve(a.port, token, a.host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
