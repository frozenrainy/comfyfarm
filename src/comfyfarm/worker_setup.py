"""Worker setup (docs/design/14): turns a Linux host into a ComfyFarm worker. Run as root, once — again to repair or to
upgrade the worker tools. Standard library only. The router serves this file with PAYLOAD filled in (the setup
parameters, the worker tools of its version, where to report); nothing here talks to the internet.

  sudo python3 worker_setup.py [--dry-run]

What it does, each step idempotent: the service account (render/video groups), the worker tools in /opt/comfyfarm/worker
(root-owned — the router cannot replace them, D-40), the systemd user unit and the instance configuration in /etc,
the instance directories, optional NFS mounts, the router's agent key with its forced command, lingering, the host
check as the account — then it reports the result and this host's ssh host keys to the router.
"""
from __future__ import annotations

import base64
import grp
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import urllib.request

PAYLOAD = None   # filled in by the router: {"spec": {...}, "files": {name: base64}, "report_url": "...", "version": "..."}

TOOLS = "/opt/comfyfarm/worker"
ETC = "/etc/comfyfarm"
UNIT = "/etc/systemd/user/comfyfarm@.service"
INSTANCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
ACCOUNT = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
MARK = "# managed by ComfyFarm worker setup — changes are overwritten"

UNIT_TEXT = """{mark}
# One instance per ComfyUI on this host: comfyfarm@<instance>, run by the service account's user manager.
[Unit]
Description=ComfyUI worker %i (ComfyFarm launcher, comfy-upgrade/1)
After=network-online.target

[Service]
EnvironmentFile=/etc/comfyfarm/%i.env
ExecStart=/usr/bin/python3 /opt/comfyfarm/worker/launcher.py
# exit 2 = the launcher refused the render: do not retry; the router reads ExecMainStatus=2 and reverts
Restart=on-failure
RestartPreventExitStatus=2
RestartSec=5
StandardOutput=append:%h/logs/%i.log
StandardError=append:%h/logs/%i.log

[Install]
WantedBy=default.target
"""


class Failed(Exception):
    pass


class Host:
    """File and command access. `root` prefixes every path and `dry` only records — the tests use both."""

    def __init__(self, root: str = "", dry: bool = False, run=None):
        self.root, self.dry, self.done = root.rstrip("/"), dry, []
        self._run = run
        self.groups_changed = False

    def p(self, path: str) -> str:
        return self.root + path

    def log(self, msg: str):
        self.done.append(msg)
        print(f"  {msg}", flush=True)

    def run(self, argv: list[str], check: bool = True, as_user: str | None = None) -> tuple[int, str]:
        if as_user:
            uid = self.uid(as_user)
            argv = ["runuser", "-u", as_user, "--", "env", f"XDG_RUNTIME_DIR=/run/user/{uid}"] + argv
        if self.dry:
            self.log("would run: " + " ".join(argv))
            return 0, ""
        if self._run:
            code, out = self._run(argv)
        else:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=300)
            code, out = p.returncode, (p.stdout + p.stderr).strip()
        if check and code != 0:
            raise Failed(f"{' '.join(argv)} → exit {code}: {out[-400:]}")
        return code, out

    def uid(self, user: str) -> int:
        try:
            return pwd.getpwnam(user).pw_uid
        except KeyError:
            return 0 if self.dry or self._run else -1

    def write(self, path: str, content: str | bytes, mode: int = 0o644, owner: str | None = None) -> bool:
        """Writes only when the content changed. Returns whether it did."""
        full = self.p(path)
        data = content.encode() if isinstance(content, str) else content
        try:
            with open(full, "rb") as f:
                if f.read() == data and (os.stat(full).st_mode & 0o7777) == mode:
                    return False
        except OSError:
            pass
        if self.dry:
            self.log(f"would write {path} ({len(data)} bytes, {oct(mode)})")
            return True
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".comfyfarm-new"
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        if owner and not self.root:
            shutil.chown(tmp, owner, owner)
        os.replace(tmp, full)
        self.log(f"wrote {path}")
        return True

    def mkdir(self, path: str, mode: int = 0o755, owner: str | None = None) -> bool:
        full = self.p(path)
        if os.path.isdir(full):
            return False
        if self.dry:
            self.log(f"would create {path}")
            return True
        os.makedirs(full, mode=mode)
        os.chmod(full, mode)
        if owner and not self.root:
            shutil.chown(full, owner, owner)
        self.log(f"created {path}")
        return True

    def read(self, path: str) -> str:
        try:
            with open(self.p(path)) as f:
                return f.read()
        except OSError:
            return ""


def validate(spec: dict):
    for key in ("instance", "account", "router_url", "art", "local"):
        if not spec.get(key):
            raise Failed(f"setup parameter missing: {key}")
    if not INSTANCE.match(spec["instance"]):
        raise Failed(f"bad instance name {spec['instance']!r}")
    if not ACCOUNT.match(spec["account"]) or spec["account"] == "root":
        raise Failed(f"bad account name {spec['account']!r}")
    for key in ("art", "local", *spec.get("paths", {})):
        path = spec.get(key) or spec["paths"].get(key)
        if not isinstance(path, str) or not path.startswith("/") or "\n" in path:
            raise Failed(f"{key}: not an absolute path: {path!r}")
    if not re.match(r"^https?://[^\s/]+", spec["router_url"]):
        raise Failed(f"router_url: {spec['router_url']!r}")


def preconditions(h: Host):
    if sys.platform != "linux" and not h.root:                   # (a test builds a fake host under a root prefix)
        raise Failed("Linux only (Windows workers are not supported yet)")
    if not h.root and os.geteuid() != 0:
        raise Failed("run as root (sudo python3 worker_setup.py)")
    if not os.path.isdir(h.p("/run/systemd/system")):
        raise Failed("systemd is not running here — the worker runs as a systemd user service")
    if sys.version_info < (3, 10):
        raise Failed(f"Python {sys.version_info[0]}.{sys.version_info[1]}: the launcher needs 3.10 or newer")
    if not os.path.exists(h.p("/usr/sbin/sshd")) and not (not h.root and shutil.which("sshd")):
        raise Failed("no ssh server: install openssh-server (the router restarts and checks the worker over ssh)")


def account(h: Host, spec: dict):
    user, home, uid = spec["account"], spec.get("home") or f"/var/lib/{spec['account']}", spec.get("uid")
    try:
        have = pwd.getpwnam(user).pw_uid
    except KeyError:
        have = None
    if have is not None and uid is not None and have != uid:
        raise Failed(f"account {user} exists with uid {have}, setup asks for {uid}: shared storage needs the same uid on "
                     "every host — change the uid in the setup, or the account on this host")
    if have is None:
        if uid is not None:
            try:
                taken = pwd.getpwuid(uid).pw_name
                raise Failed(f"uid {uid} is already used by {taken!r} on this host — pick another uid for the farm")
            except KeyError:
                pass
        # a real shell: sshd runs the forced command through it (nologin would refuse the router's agent key)
        h.run(["useradd", "--system", "--create-home", "--home-dir", home, "--shell", "/bin/sh", user]
              + (["--uid", str(uid)] if uid is not None else []))
        h.log(f"account {user} created (home {home}" + (f", uid {uid})" if uid is not None else ")"))
    for g in ("render", "video"):
        try:
            members = grp.getgrnam(g).gr_mem
        except KeyError:
            continue                                             # no such group on this host: no GPU device nodes
        if user not in members:
            h.run(["usermod", "-aG", g, user])
            h.log(f"{user} added to {g}")
            h.groups_changed = True
    h.mkdir(f"{home}/logs", 0o750, owner=user)
    return home


def tools(h: Host, files: dict, version: str):
    h.mkdir(TOOLS)
    for name, b64 in sorted(files.items()):
        if not re.fullmatch(r"[a-z_]+\.py", name):
            raise Failed(f"unexpected file in the payload: {name!r}")
        h.write(f"{TOOLS}/{name}", base64.b64decode(b64), 0o644)
    h.write(f"{TOOLS}/VERSION", version + "\n", 0o644)


def configure(h: Host, spec: dict, home: str):
    inst, user = spec["instance"], spec["account"]
    paths = spec.get("paths", {})
    h.mkdir(ETC)
    h.write(UNIT, UNIT_TEXT.format(mark=MARK))
    prefixes = sorted({p for k, p in paths.items() if k in ("models", "input", "output", "user", "temp")})
    h.write(f"{ETC}/{inst}.env", "\n".join([
        MARK,
        f"COMFY_LAUNCH_ROUTER={spec['router_url'].rstrip('/')}",
        f"COMFY_LAUNCH_INSTANCE={inst}",
        f"COMFY_LAUNCH_ARTIFACTS={spec['art']}",
        f"COMFY_LAUNCH_LOCAL={spec['local']}",
        f"COMFY_LAUNCH_PREFIXES={','.join(prefixes)}", ""]))
    h.write(f"{ETC}/{inst}.check.json", json.dumps({
        "gpu": spec.get("gpu", "any"), "paths": paths, "port": spec.get("port", 8188),
        "listen": spec.get("listen", "0.0.0.0"), "account": user, "uid": spec.get("uid")}, indent=1) + "\n")
    served = set()
    for line in h.read(f"{ETC}/agent.env").splitlines():
        if line.startswith("COMFYFARM_AGENT_INSTANCES="):
            served = {x for x in line.split("=", 1)[1].split(",") if x}
    h.write(f"{ETC}/agent.env", "\n".join([MARK, f"COMFYFARM_AGENT_INSTANCES={','.join(sorted(served | {inst}))}",
                                           "COMFYFARM_AGENT_USER=1", ""]))


def directories(h: Host, spec: dict):
    """After the mounts: an instance directory on shared storage is created there, not hidden under the mount point."""
    user, paths = spec["account"], spec.get("paths", {})
    h.mkdir(spec["local"], 0o755, owner=user)
    for key in ("input", "output", "user", "temp"):
        if paths.get(key):
            h.mkdir(paths[key], 0o775, owner=user)


def mounts(h: Host, spec: dict):
    entries = spec.get("mounts") or []
    if not entries:
        return
    have = any(os.path.exists(h.p(p)) for p in ("/sbin/mount.nfs", "/usr/sbin/mount.nfs")) or (not h.root and shutil.which("mount.nfs"))
    if not have and not h.dry:
        raise Failed("NFS mounts were asked for but mount.nfs is missing: install nfs-common from your local mirror")
    fstab = h.read("/etc/fstab")
    for m in entries:
        src, target = m["source"], m["target"]
        if not re.fullmatch(r"[A-Za-z0-9._-]+:/[^\s]*", src) or not target.startswith("/") or " " in target:
            raise Failed(f"bad mount {src} → {target}")
        opts = ",".join(["defaults", "_netdev", "nofail"] + ([m["options"]] if m.get("options") else []))
        h.mkdir(target)
        if not any(line.split()[:2] == [src, target] for line in fstab.splitlines() if line.strip() and not line.startswith("#")):
            fstab = fstab.rstrip("\n") + f"\n{src} {target} nfs {opts} 0 0  {MARK}\n"
            h.write("/etc/fstab", fstab)
        code, _ = h.run(["mountpoint", "-q", target], check=False)
        if code != 0:
            h.run(["mount", target])
            h.log(f"mounted {src} on {target}")


def ssh_access(h: Host, spec: dict, home: str):
    key = spec.get("agent_pubkey", "").strip()
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+( [^\n]*)?", key):
        raise Failed("the router's agent key is missing or not an ed25519 public key")
    forced = (f'command="/usr/bin/python3 {TOOLS}/agent.py ssh",no-port-forwarding,no-pty,no-agent-forwarding,'
              f"no-X11-forwarding,no-user-rc {key}")
    # root-owned: the account (and so the router) cannot add keys or lift the forced command
    h.mkdir(f"{home}/.ssh", 0o755)
    h.write(f"{home}/.ssh/authorized_keys", f"{MARK}\n{forced}\n", 0o644)


def user_manager(h: Host, spec: dict):
    user, inst = spec["account"], spec["instance"]
    h.run(["loginctl", "enable-linger", user])
    uid = h.uid(user)
    # a user manager started before the account got render/video keeps its old groups (no GPU) until restarted
    h.run(["systemctl", "restart" if h.groups_changed else "start", f"user@{uid}.service"])
    h.run(["systemctl", "--user", "daemon-reload"], as_user=user)
    h.run(["systemctl", "--user", "enable", f"comfyfarm@{inst}"], as_user=user)   # started by the router, not here


def host_check(h: Host, spec: dict) -> list[dict]:
    code, out = h.run(["/usr/bin/python3", f"{TOOLS}/hostcheck.py", spec["instance"], "--json"], check=False,
                      as_user=spec["account"])
    try:
        return json.loads(out) if out else []
    except ValueError:
        return [{"level": "FAIL", "name": "host check", "detail": f"did not run (exit {code}): {out[-300:]}"}]


def host_keys(h: Host) -> list[str]:
    keys = []
    for name in sorted(os.listdir(h.p("/etc/ssh"))) if os.path.isdir(h.p("/etc/ssh")) else []:
        if name.startswith("ssh_host_") and name.endswith("_key.pub"):
            parts = h.read(f"/etc/ssh/{name}").split()
            if len(parts) >= 2:
                keys.append(f"{parts[0]} {parts[1]}")
    return keys


def report(url: str, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def setup(payload: dict, h: Host) -> dict:
    spec = payload["spec"]
    steps, name = [], "parameters"
    result = {"hostname": socket.gethostname(), "version": payload.get("version", ""), "steps": steps}
    try:
        validate(spec)
        for name, fn in [("preconditions", lambda: preconditions(h)),
                         ("account", lambda: result.__setitem__("home", account(h, spec))),
                         ("worker tools", lambda: tools(h, payload.get("files", {}), payload.get("version", ""))),
                         ("configuration", lambda: configure(h, spec, result["home"])),
                         ("mounts", lambda: mounts(h, spec)),
                         ("directories", lambda: directories(h, spec)),
                         ("router access", lambda: ssh_access(h, spec, result["home"])),
                         ("user manager", lambda: user_manager(h, spec))]:
            print(f"- {name}", flush=True)
            fn()
            steps.append({"step": name, "ok": True})
        print("- host check", flush=True)
        result["check"] = host_check(h, spec)
        result["ok"] = not any(r["level"] == "FAIL" for r in result["check"])
    except Failed as e:
        steps.append({"step": name, "ok": False, "error": str(e)})
        result["ok"], result["error"] = False, str(e)
        print(f"  FAILED: {e}", file=sys.stderr, flush=True)
    result["host_keys"] = host_keys(h)
    return result


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in args
    if PAYLOAD is None:
        print("worker_setup.py: download it from the dashboard (System → Workers → Set up a worker); this copy has no "
              "setup parameters", file=sys.stderr)
        return 2
    print(f"ComfyFarm worker setup {PAYLOAD.get('version', '')} — instance {PAYLOAD['spec'].get('instance')}"
          + (" (dry run: nothing is changed)" if dry else ""), flush=True)
    result = setup(PAYLOAD, Host(dry=dry))
    for r in result.get("check", []):
        print(f"  {r['level']:<5}{r['name']:<18}{r['detail']}")
    if not dry and PAYLOAD.get("report_url"):
        try:
            report(PAYLOAD["report_url"], result)
            print("reported to the router — continue in the dashboard")
        except Exception as e:                                   # noqa: BLE001 — the operator needs to see it
            print(f"could not report to the router ({type(e).__name__}: {e}); the dashboard will not see this run",
                  file=sys.stderr)
            return 1
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
