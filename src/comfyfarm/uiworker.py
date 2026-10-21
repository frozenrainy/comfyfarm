"""The UI worker as a container (docs/design/15): the `ui` service of the compose deployment. The same launcher as on
a worker host — it fetches this instance's render from the router and runs ComfyUI from published artifacts — under a
small supervisor that does what systemd does on a host, and the agent's HTTP side (restart / status / check) on the
compose network, so the router restarts and rolls it out like any other worker.

  restart policy   as the host's unit: Restart=on-failure, RestartPreventExitStatus=2 (the launcher refused the render:
                   stay down, the router reads ExecMainStatus=2 and reverts), RestartSec=5
  allow-list       COMFYFARM_ALLOW_NETWORK = the compose network(s) this container is on (the router and the gateway
                   reach it there; its port is not published), added to whatever the profile's COMFYFARM_ALLOW says

Environment: COMFYFARM_ROUTER (http://router:8188), COMFYFARM_ARTIFACTS (the store, at the workers' path),
COMFYFARM_SHARED (input/output/user below it — the storage the GPU workers use), COMFYFARM_UI_HOME (this container's
own: local copies, models, temp), COMFYFARM_UI_INSTANCE (ui-1), COMFYFARM_UI_TOKEN_FILE (the agent token).
"""
from __future__ import annotations

import ipaddress
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request

from . import agent

HERE = os.path.dirname(os.path.abspath(__file__))
RESTART_SEC = 5.0
STOP_TIMEOUT = 20.0


class Supervisor:
    def __init__(self, argv: list[str], env: dict, restart_sec: float = RESTART_SEC):
        self.argv, self.env, self.restart_sec = argv, env, restart_sec
        self.proc: subprocess.Popen | None = None
        self.rc: int | None = None              # the last exit code; None while running or never started
        self.restarts = 0
        self.lock = threading.RLock()
        self.generation = 0                     # a stop/start in between makes a pending automatic restart moot

    def start(self) -> None:
        with self.lock:
            self.rc = None
            self.generation += 1
            self.proc = subprocess.Popen(self.argv, env=self.env)
            threading.Thread(target=self._watch, args=(self.proc, self.generation), daemon=True).start()

    def _watch(self, proc: subprocess.Popen, generation: int) -> None:
        rc = proc.wait()
        with self.lock:
            if generation != self.generation:
                return                          # stopped on purpose (restart): the new one is watched by its own thread
            self.rc = rc
        if rc not in (0, 2):                    # on-failure, but not a refusal
            time.sleep(self.restart_sec)
            with self.lock:
                if generation == self.generation:
                    self.restarts += 1
                    self.start()

    def stop(self) -> None:
        with self.lock:
            self.generation += 1
            p = self.proc
        if p and p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def restart(self) -> None:
        self.stop()
        self.start()

    def status(self) -> dict:
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            rc = self.rc if not running else None
        return {"ActiveState": "active" if running else ("failed" if rc not in (0, None) else "inactive"),
                "ExecMainStatus": str(rc if rc is not None else 0), "NRestarts": str(self.restarts)}


def own_networks() -> list[str]:
    """The IPv4 networks of this container's interfaces (SIOCGIFADDR / SIOCGIFNETMASK): the compose network(s)."""
    import fcntl
    out = []
    for name in sorted(os.listdir("/sys/class/net")) if os.path.isdir("/sys/class/net") else []:
        if name == "lo":
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            req = struct.pack("256s", name[:15].encode())
            addr = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, req)[20:24])
            mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x891B, req)[20:24])
            out.append(str(ipaddress.ip_network(f"{addr}/{mask}", strict=False)))
        except OSError:
            continue
        finally:
            s.close()
    return out


def layout(env: dict) -> dict:
    """Where everything is, from the environment: what the router puts into this instance's configuration too."""
    home, shared = env.get("COMFYFARM_UI_HOME", "/var/lib/comfyfarm-ui"), env.get("COMFYFARM_SHARED", "")
    inst = env.get("COMFYFARM_UI_INSTANCE", "ui-1")
    return {"instance": inst, "art": env.get("COMFYFARM_ARTIFACTS", ""), "local": f"{home}/local",
            "models": f"{home}/models", "temp": f"{home}/temp/{inst}", "input": f"{shared}/input",
            "output": f"{shared}/output", "user": f"{shared}/user", "etc": f"{home}/etc", "home": home}


def prepare(env: dict) -> dict:
    lay = layout(env)
    for key in ("local", "models", "temp", "input", "output", "user", "etc"):
        os.makedirs(lay[key], exist_ok=True)
    paths = {k: lay[k] for k in ("art", "local", "models", "input", "output", "user", "temp")}
    # what the host check reads on a host (/etc/comfyfarm/<instance>.env, .check.json), here in the container's home
    with open(os.path.join(lay["etc"], f"{lay['instance']}.env"), "w") as f:
        f.write(f"COMFY_LAUNCH_ROUTER={env['COMFYFARM_ROUTER']}\nCOMFY_LAUNCH_INSTANCE={lay['instance']}\n"
                f"COMFY_LAUNCH_ARTIFACTS={lay['art']}\nCOMFY_LAUNCH_LOCAL={lay['local']}\n")
    with open(os.path.join(lay["etc"], f"{lay['instance']}.check.json"), "w") as f:
        json.dump({"gpu": "none", "role": "ui", "port": 8189, "listen": "0.0.0.0", "paths": paths, "container": True}, f)
    return lay


def launcher_env(env: dict, lay: dict) -> dict:
    out = {k: v for k, v in env.items() if not k.startswith("COMFYFARM_UI_")}
    out.update({"COMFY_LAUNCH_ROUTER": env["COMFYFARM_ROUTER"], "COMFY_LAUNCH_INSTANCE": lay["instance"],
                "COMFY_LAUNCH_ARTIFACTS": lay["art"], "COMFY_LAUNCH_LOCAL": lay["local"],
                "COMFY_LAUNCH_PREFIXES": ",".join(lay[k] for k in ("input", "output", "user", "models", "temp")),
                "HOME": lay["home"], "USER": out.get("USER") or "comfyfarm", "LOGNAME": out.get("LOGNAME") or "comfyfarm"})
    if not out.get("COMFYFARM_ALLOW_NETWORK"):
        out["COMFYFARM_ALLOW_NETWORK"] = ",".join(own_networks())
    return out


def wait_for_router(url: str, seconds: float = 120.0) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/api/health/dispatch", timeout=5):
                return
        except Exception:
            time.sleep(2)


def make_do(sup: Supervisor, lay: dict, env: dict):
    def do(verb: str, instance: str) -> tuple[int, str]:
        if instance != lay["instance"]:
            raise agent.Refused(f"instance {instance!r} is not served here")
        if verb == "restart":
            threading.Thread(target=sup.restart, daemon=True).start()     # delivery only, like the host's agent
            return 0, "restart requested"
        if verb == "status":
            return 0, json.dumps(sup.status())
        if verb == "check":
            p = subprocess.run([sys.executable, os.path.join(HERE, "hostcheck.py"), instance, "--json"], capture_output=True,
                               text=True, timeout=180, env={**env, "COMFYFARM_ETC": lay["etc"], "USER": env.get("USER") or "comfyfarm"})
            if p.returncode not in (0, 1) or not p.stdout.strip():         # it did not run: say why, not "no output"
                return p.returncode or 1, (p.stderr or p.stdout).strip()[-2000:] or "the host check printed nothing"
            return 0, p.stdout.strip()
        raise agent.Refused(f"unknown command {verb!r}")
    return do


def main(argv=None) -> int:
    env = dict(os.environ)
    for k in ("COMFYFARM_ROUTER", "COMFYFARM_ARTIFACTS", "COMFYFARM_SHARED"):
        if not env.get(k):
            print(f"comfyfarm ui-worker: set {k}", file=sys.stderr)
            return 2
    token_file = env.get("COMFYFARM_UI_TOKEN_FILE", "/run/comfyfarm-ui-agent/token")
    with open(token_file) as f:
        token = f.read().strip()
    lay = prepare(env)
    sup = Supervisor([sys.executable, os.path.join(HERE, "launcher.py")], launcher_env(env, lay))
    threading.Thread(target=agent.serve, args=(8399, token, "0.0.0.0", make_do(sup, lay, env)), daemon=True).start()
    print(f"comfyfarm ui-worker: instance {lay['instance']}, store {lay['art']}, shared {env['COMFYFARM_SHARED']}; "
          f"agent on :8399", flush=True)
    wait_for_router(env["COMFYFARM_ROUTER"])
    sup.start()                       # nothing assigned yet: the launcher refuses (exit 2) and waits for the router's start
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    sup.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
