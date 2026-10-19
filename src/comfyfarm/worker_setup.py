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
BUILD_USER, BUILD_HOME = "comfyfarm-build", "/var/lib/comfyfarm-build"     # BUILD_HOME: root; the account's own home: /home below
BUILD_UNITS = ("/etc/systemd/system/comfyfarm-build.path", "/etc/systemd/system/comfyfarm-build.service")
BUILD_FILE = re.compile(r"^(publish\.py|build\.py|recipes/[A-Za-z0-9][A-Za-z0-9._-]*\.json|"
                        r"(worker_ext/comfyfarm_worker|ui_ext/comfyfarm_ui)(/[A-Za-z0-9_][A-Za-z0-9._-]*)+)$")

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
            # as the account: from /, not root's working directory (ROCm aborts when it cannot read its cwd)
            p = subprocess.run(argv, capture_output=True, text=True, timeout=300, cwd="/" if as_user else None)
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

    def remove(self, path: str) -> bool:
        if not os.path.exists(self.p(path)):
            return False
        if self.dry:
            self.log(f"would remove {path}")
            return True
        os.remove(self.p(path))
        self.log(f"removed {path}")
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


ADMIN_GROUPS = ("sudo", "wheel", "admin")


def owner_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid {uid} (no account of that number here)"


def has_password(user: str, root: str = "") -> bool:
    """A usable password in /etc/shadow — a person's account; a service account is locked (!, *, empty)."""
    try:
        with open(root + "/etc/shadow") as f:
            for line in f:
                parts = line.split(":")
                if parts[0] == user:
                    return len(parts) > 1 and bool(parts[1]) and not parts[1].startswith(("!", "*"))
    except OSError:
        pass
    return False


def person_account(pw, root: str = "") -> str | None:
    """Why an existing account cannot be the service account (None: it can). The service account runs ComfyUI and gets
    a root-owned authorized_keys holding only the router's forced-command key: a person's login account would lose its
    own keys, and the router would act as someone who may use sudo. A person is known by what the account has — root,
    sudo, a password, keys of its own (checked by the caller) — not by where its home is: a service account made by
    hand (`useradd -m comfyfarm`, a manual install under /home) must be adoptable."""
    if pw.pw_uid == 0 or pw.pw_dir == "/root":
        return "root"
    if has_password(pw.pw_name, root):
        return "a person's login account (it has a password)"
    for g in ADMIN_GROUPS:
        try:
            gr = grp.getgrnam(g)
        except KeyError:
            continue
        if pw.pw_name in gr.gr_mem or pw.pw_gid == gr.gr_gid:
            return f"allowed to use sudo (group {g})"
    return None


def account(h: Host, spec: dict):
    user, uid = spec["account"], spec.get("uid")
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        pw = None
    if pw is not None and (why := person_account(pw, h.root)):
        raise Failed(f"{user} is {why} — the service account must be its own: it runs ComfyUI and only the router's "
                     "restart key logs in as it. Keep the default 'comfyfarm' (setup creates it) or name an unused account")
    home = pw.pw_dir if pw is not None else spec.get("home") or f"/var/lib/{user}"    # an existing account: its own home
    keys = f"{home}/.ssh/authorized_keys"
    if pw is not None and os.path.exists(h.p(keys)) and MARK not in h.read(keys):      # before the first change
        raise Failed(f"{keys} holds keys ComfyFarm did not write — {user} is someone's account, not replaced. "
                     "Keep the default 'comfyfarm' (setup creates it); if this one really is the service account, "
                     "move those keys away and run setup again")
    have = pw.pw_uid if pw is not None else None
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
    prev = spec.get("previous_account")
    if prev and prev != user and ACCOUNT.match(prev):          # set up again with another account: one account per instance
        try:
            old_home = pwd.getpwnam(prev).pw_dir
        except KeyError:
            old_home = None
        if old_home:
            old_keys = f"{old_home}/.ssh/authorized_keys"
            if os.path.exists(h.p(old_keys)) and MARK in h.read(old_keys):
                h.remove(old_keys)
                h.log(f"the router's key removed from {prev} (this worker's account is {user} now)")
            h.run(["systemctl", "--user", "disable", "--now", f"comfyfarm@{spec['instance']}"], check=False, as_user=prev)
    return home


def tools(h: Host, files: dict, version: str):
    h.mkdir(TOOLS)
    for name, b64 in sorted(files.items()):
        if not re.fullmatch(r"[a-z_]+\.py", name):
            raise Failed(f"unexpected file in the payload: {name!r}")
        h.write(f"{TOOLS}/{name}", base64.b64decode(b64), 0o644)
    h.write(f"{TOOLS}/VERSION", version + "\n", 0o644)


def configure(h: Host, spec: dict, home: str):
    own = f"{home}/.config/systemd/user/comfyfarm@.service"
    if os.path.exists(h.p(own)):
        raise Failed(f"{own} would take the place of the unit setup installs (/etc/systemd/user) — remove it "
                     "(left from a manual install?) and run setup again")
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
                                           "COMFYFARM_AGENT_USER=1"]
                                          + (["COMFYFARM_AGENT_BUILD=1"] if spec.get("builds") else []) + [""]))


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
        listed = any(line.split()[:2] == [src, target] for line in fstab.splitlines() if line.strip() and not line.startswith("#"))
        code, _ = h.run(["mountpoint", "-q", target], check=False)
        if code == 0 and not listed:
            # the site mounted something there already (rehearsal: a mount line carried over from another host's setup
            # would have stacked an NFS mount on the site's own after the next boot)
            h.log(f"{target} is already mounted by this host — left as it is, nothing added to /etc/fstab")
            continue
        h.mkdir(target)
        if not listed:
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
    path = f"{home}/.ssh/authorized_keys"
    if os.path.exists(h.p(path)) and MARK not in h.read(path):
        raise Failed(f"{path} holds keys ComfyFarm did not write — not replaced. The service account must be its own "
                     "(keep 'comfyfarm'); if this one is, move those keys away and run setup again")
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


def builds(h: Host, spec: dict, files: dict):
    """Builds from the dashboard (opt-in, build.py): the recipes, publish.py and the extensions root-owned under TOOLS,
    an account that owns the artifact store and runs `publish.py recipe`, a drop box the service account can put a
    recipe name into, and the units that take it. Off: the units go, the account and its files stay."""
    if not spec.get("builds"):
        if any(os.path.exists(h.p(u)) for u in BUILD_UNITS):
            h.run(["systemctl", "disable", "--now", "comfyfarm-build.path"], check=False)
            for u in BUILD_UNITS:
                h.remove(u)
            h.run(["systemctl", "daemon-reload"])
            h.log("builds from the dashboard: turned off")
        return
    if not files:
        raise Failed("this router sent no build tools — upgrade the router, or set the host up without builds")
    uv = next((u for u in ("/usr/local/bin/uv", "/usr/bin/uv") if os.path.exists(h.p(u))), None)
    if not uv:
        raise Failed("builds need uv where every account finds it: curl -LsSf https://astral.sh/uv/install.sh | "
                     "sudo env UV_INSTALL_DIR=/usr/local/bin sh   (then run setup again)")
    if not os.path.exists(h.p("/usr/bin/git")):
        raise Failed("builds need git (apt install git)")
    for rel, b64 in sorted(files.items()):
        if not BUILD_FILE.match(rel) or "/." in "/" + rel:
            raise Failed(f"unexpected file in the build payload: {rel!r}")
        h.mkdir(os.path.dirname(f"{TOOLS}/{rel}"))
        h.write(f"{TOOLS}/{rel}", base64.b64decode(b64), 0o644)
    try:
        pw = pwd.getpwnam(BUILD_USER)
    except KeyError:
        pw = None
    if pw is not None and (why := person_account(pw, h.root)):
        raise Failed(f"{BUILD_USER} is {why} — the build account must be its own")
    home = f"{BUILD_HOME}/home"
    if pw is None:
        h.run(["useradd", "--system", "--no-create-home", "--home-dir", home, "--shell", "/usr/sbin/nologin", BUILD_USER])
        h.log(f"account {BUILD_USER} created (home {home}) — builds run as it")
    art = spec["art"]
    full = h.p(art)
    if not os.path.isdir(full):
        h.mkdir(art, 0o755, owner=BUILD_USER)
    elif not h.dry and os.stat(full).st_uid != h.uid(BUILD_USER):
        owner = owner_name(os.stat(full).st_uid)
        if os.listdir(full):
            raise Failed(f"{art} belongs to {owner}: builds from the dashboard publish there as {BUILD_USER} — "
                         f"chown -R {BUILD_USER} {art} once, or set the host up without builds "
                         f"({spec['account']}, which runs ComfyUI, must not be able to write it)")
        h.run(["chown", BUILD_USER, art])
        h.log(f"{art} (empty) given to {BUILD_USER}")
    # Root owns BUILD_HOME, status/ and requests/: what root writes there is never a link the build account made, and
    # the service account only passes through (0711) to drop a request (0730, group = its own) or read a state.
    # The build account's own home is home/ (uv's interpreter store, caches). An older setup made BUILD_HOME the
    # account's home and gave it status/: taken back.
    if not h.dry and os.path.isdir(h.p(BUILD_HOME)) and os.stat(h.p(BUILD_HOME)).st_uid != 0:
        h.run(["chown", "root:root", BUILD_HOME])
        for sub in ("status", "requests"):
            if os.path.isdir(h.p(f"{BUILD_HOME}/{sub}")) and os.stat(h.p(f"{BUILD_HOME}/{sub}")).st_uid != 0:
                shutil.rmtree(h.p(f"{BUILD_HOME}/{sub}"))
                h.log(f"{BUILD_HOME}/{sub} from an older setup removed (root makes it anew)")
        h.run(["usermod", "--home", home, BUILD_USER])
    h.mkdir(BUILD_HOME, 0o711)
    h.run(["chmod", "0711", BUILD_HOME])
    h.mkdir(home, 0o700, owner=BUILD_USER)
    h.mkdir(f"{BUILD_HOME}/status", 0o755)
    h.mkdir(f"{BUILD_HOME}/requests", 0o730)
    h.run(["chgrp", spec["account"], f"{BUILD_HOME}/requests"])
    h.run(["chmod", "0730", f"{BUILD_HOME}/requests"])
    h.write(BUILD_UNITS[0], f"""{MARK}
[Unit]
Description=ComfyFarm: a build requested from the dashboard

[Path]
DirectoryNotEmpty={BUILD_HOME}/requests

[Install]
WantedBy=multi-user.target
""")
    h.write(BUILD_UNITS[1], f"""{MARK}
[Unit]
Description=ComfyFarm: build the requested recipes (publish.py recipe as {BUILD_USER})

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {TOOLS}/build.py run {art} {uv}
""")
    h.run(["systemctl", "daemon-reload"])
    h.run(["systemctl", "enable", "--now", "comfyfarm-build.path"])


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
                         ("user manager", lambda: user_manager(h, spec)),
                         ("builds", lambda: builds(h, spec, payload.get("build_files", {})))]:
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
