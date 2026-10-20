"""Host check for a worker (docs/design/14): is this host ready to run a ComfyFarm instance? Read-only; standard
library only — installed next to the launcher by worker setup and run as the service account.

  python3 hostcheck.py <instance> [--json]

Configuration: /etc/comfyfarm/<instance>.env (the launcher's) and /etc/comfyfarm/<instance>.check.json (what setup
was asked for: expected GPU, the instance's directories, port). Prints one line per check — OK / WARN / FAIL — or a
JSON list for the router. Exit 1 when anything FAILs.
"""
from __future__ import annotations

import glob
import grp
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

ETC = os.environ.get("COMFYFARM_ETC", "/etc/comfyfarm")     # the UI worker container keeps them in its home
OK, WARN, FAIL = "OK", "WARN", "FAIL"
LOCAL_FREE_WARN = 30 * 2**30                # a core + node copies + run files; venvs stay on the store
MIN_PYTHON = (3, 10)
# shared libraries GPU wheels load from the system, and the Debian/Ubuntu package that has each
SYSTEM_LIBS = {"libatomic.so.1": "libatomic1", "libgomp.so.1": "libgomp1", "libnuma.so.1": "libnuma1",
               "libdrm.so.2": "libdrm2", "libdrm_amdgpu.so.1": "libdrm-amdgpu1", "libelf.so.1": "libelf1t64",
               "libtinfo.so.6": "libtinfo6", "libstdc++.so.6": "libstdc++6", "libquadmath.so.0": "libquadmath0"}


def read_env(path: str) -> dict:
    out = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def run_cmd(argv: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 127, f"{type(e).__name__}: {e}"


class Check:
    """`root` prefixes the device and system paths (/dev, /sys, /var/lib/systemd) so tests can build a fake host."""

    def __init__(self, env: dict, cfg: dict, *, root: str = "", cmd=run_cmd, user: str | None = None):
        self.env, self.cfg, self.root, self.cmd = env, cfg, root.rstrip("/"), cmd
        self.user = user or pwd.getpwuid(os.getuid()).pw_name
        self.out: list[dict] = []

    def add(self, level: str, name: str, detail: str, **data):
        self.out.append({"level": level, "name": name, "detail": detail, **data})

    def p(self, path: str) -> str:
        return self.root + path

    # ---- system ---------------------------------------------------------------------------------------------------
    def system(self):
        v = sys.version_info
        self.add(OK if v[:2] >= MIN_PYTHON else FAIL, "python", f"{v[0]}.{v[1]}.{v[2]} for the launcher"
                 + ("" if v[:2] >= MIN_PYTHON else f" — needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer"))
        if self.cfg.get("container"):        # the UI worker container: its supervisor is the service manager
            return
        linger = os.path.exists(self.p(f"/var/lib/systemd/linger/{self.user}"))
        self.add(OK if linger else FAIL, "lingering", f"{self.user}: " + ("on — the instance starts at boot" if linger else
                 "off — the user manager stops at logout (setup runs `loginctl enable-linger`)"))
        code, out = self.cmd(["systemctl", "--user", "is-system-running"])
        state = out.splitlines()[-1] if out else ""
        self.add(OK if state in ("running", "degraded") else FAIL, "user manager",
                 f"systemd --user: {state or 'not reachable'}")

    # ---- GPU ------------------------------------------------------------------------------------------------------
    def gpu(self):
        want = self.cfg.get("gpu", "any")                              # amd | nvidia | none | any
        found = []
        kfd = self.p("/dev/kfd")
        if os.path.exists(kfd):
            renders = sorted(glob.glob(self.p("/dev/dri/renderD*")))
            usable = os.access(kfd, os.R_OK | os.W_OK) and any(os.access(r, os.R_OK | os.W_OK) for r in renders)
            targets = []
            for props in sorted(glob.glob(self.p("/sys/class/kfd/kfd/topology/nodes/*/properties"))):
                with open(props) as f:
                    m = re.search(r"^gfx_target_version (\d+)", f.read(), re.M)
                if m and int(m.group(1)):
                    n = int(m.group(1))
                    targets.append(f"gfx{n // 10000}{(n // 100) % 100:x}{n % 100:x}")
            ver = ""
            if os.path.exists(self.p("/sys/module/amdgpu/version")):
                with open(self.p("/sys/module/amdgpu/version")) as f:
                    ver = f" · amdgpu {f.read().strip()}"
            if usable:
                found.append("amd")
                self.add(OK, "GPU (AMD)", f"{len(targets)} GPU(s): {', '.join(targets) or 'unknown target'}{ver}")
            else:
                self.add(FAIL, "GPU (AMD)", f"/dev/kfd present but {self.user} cannot open it or a render node — "
                         "add the account to the render and video groups (setup does), then log in again")
        if os.path.exists(self.p("/dev/nvidiactl")):
            if not os.access(self.p("/dev/nvidiactl"), os.R_OK | os.W_OK):
                self.add(FAIL, "GPU (NVIDIA)", f"/dev/nvidiactl present but {self.user} cannot open it")
            else:
                code, out = self.cmd(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])
                if code == 0 and out:
                    found.append("nvidia")
                    self.add(OK, "GPU (NVIDIA)", "; ".join(out.splitlines()))
                else:
                    self.add(FAIL, "GPU (NVIDIA)", f"nvidia-smi failed: {out[:200] or code} — driver not loaded?")
        self.found = found
        if want in ("amd", "nvidia") and want not in found:
            self.add(FAIL, "GPU", f"setup expects an {want.upper()} GPU and none is usable here (driver installed and loaded?)")
        elif not found and want == "any":
            self.add(WARN, "GPU", "no GPU found — this host can only run ComfyUI on the CPU")

    def builds(self):
        """Builds from the dashboard, if setup allowed them here: the recipes root installed (the only ones the router
        may name)."""
        try:
            allowed = read_env(self.p("/etc/comfyfarm/agent.env")).get("COMFYFARM_AGENT_BUILD") == "1"
        except OSError:
            return
        if not allowed:
            return
        d = self.p("/opt/comfyfarm/worker/recipes")
        names = sorted(n[:-5] for n in os.listdir(d) if n.endswith(".json")) if os.path.isdir(d) else []
        self.add(OK if names else WARN, "builds", "from the dashboard: " + (", ".join(names) if names else
                 "allowed, but no recipe is installed — run setup again") + " (built as comfyfarm-build)", recipes=names)

    def recipes(self, art: str):
        """Profile templates that `publish.py recipe` left in the store: the dashboard makes profiles from them."""
        templates = []
        for f in sorted(glob.glob(os.path.join(art, "profiles", "*.json"))):
            try:
                with open(f) as fh:
                    t = json.load(fh)
            except (OSError, ValueError):
                continue
            roles = [k for k in ("gpu", "ui") if isinstance(t, dict) and isinstance(t.get(k), dict)]
            if roles and isinstance(t.get("template"), str):                  # a template may hold one half (ui only)
                missing = sorted({f"{kind}/{n}" for role in roles
                                  for kind, names in (("core", [t[role].get("core")]), ("venv", [t[role].get("venv")]),
                                                      ("nodes", t[role].get("nodes") or []))
                                  for n in names if isinstance(n, str) and not os.path.exists(os.path.join(art, kind, n, "READY"))})
                templates.append({**t, "missing": missing})
        if templates:
            usable = [t["template"] for t in templates if not t["missing"]]
            self.add(OK if usable else WARN, "recipes", ", ".join(usable or [t["template"] + " (artifacts missing)" for t in templates])
                     + " — System → Rollout → Profiles → From a recipe", templates=templates)
        else:
            self.add(WARN, "recipes", f"no profile template in {art} — on a host with internet that mounts it here: "
                     f"python3 publish.py recipe {art} rocm-gfx1201 (docs/guide/rollouts.md §1)")

    # ---- paths ----------------------------------------------------------------------------------------------------
    def paths(self):
        art, local = self.env.get("COMFY_LAUNCH_ARTIFACTS", ""), self.env.get("COMFY_LAUNCH_LOCAL", "")
        published = len(glob.glob(os.path.join(art, "*", "*", "READY"))) if art else 0
        if not art or not os.path.isdir(art) or not os.access(art, os.R_OK | os.X_OK):
            self.add(FAIL, "artifact store", f"{art or '(not set)'} is not readable — is the store mounted here, at the "
                     "same path as on the other hosts?")
        else:
            self.add(OK if published else WARN, "artifact store", f"{art}: {published} published artifact(s)"
                     + ("" if published else " — publish ComfyUI, a venv and the node set before assigning a profile"))
            self.recipes(art)
        if not local or not os.path.isdir(local) or not os.access(local, os.W_OK):
            self.add(FAIL, "local root", f"{local or '(not set)'} is not writable by {self.user}")
        else:
            free = shutil.disk_usage(local).free
            self.add(OK if free >= LOCAL_FREE_WARN else WARN, "local root",
                     f"{local}: {free / 2**30:.0f} GB free" + ("" if free >= LOCAL_FREE_WARN else " (a core and node copies need room)"))
        dirs = self.cfg.get("paths", {})
        for key in ("input", "output", "user", "temp"):
            d = dirs.get(key)
            if not d:
                continue
            ok = os.path.isdir(d) and os.access(d, os.W_OK | os.X_OK)
            who = f"{self.user} (uid {os.getuid()})" if not self.root else self.user
            self.add(OK if ok else FAIL, f"{key} directory", f"{d}" + ("" if ok else f" — missing or not writable by {who}; on "
                     "shared storage give that uid write access there (every host uses the same uid)"))
        if dirs.get("models"):
            m = dirs["models"]
            ok = os.path.isdir(m) and os.access(m, os.R_OK | os.X_OK)
            self.add(OK if ok else FAIL, "models", f"{m}" + ("" if ok else " — missing or not readable"))
        try:
            try:
                import launcher                                          # on a worker: installed next to this file
            except ImportError:
                from comfyfarm import launcher                           # in the package (tests, the router's copy)
            code = [art] + [f"{local}/{d}" for d in launcher.MANAGED_LOCAL]
            for key in ("input", "output", "user", "temp"):
                d = dirs.get(key)
                if d and art and local and (launcher.under(d, code) or any(launcher.under(c, [d]) for c in code)):
                    self.add(FAIL, f"{key} directory", f"{d} overlaps <ART> or the code under <LOCAL> — the launcher "
                             "will refuse every render (contract §4)")
        except ImportError:
            pass
        if dirs.get("output") and os.access(dirs["output"], os.W_OK):
            marker = os.path.join(dirs["output"], ".comfyfarm-hosts", socket.gethostname())
            try:
                os.makedirs(os.path.dirname(marker), exist_ok=True)
                with open(marker, "w") as f:
                    f.write(f"{int(time.time())}\n")
            except OSError:
                pass                                                     # the directory check above says why

    # ---- venvs ----------------------------------------------------------------------------------------------------
    def venvs(self):
        """Each published venv must not only start but import torch here: the wheels need system libraries the build
        host had and a fresh host may lack (ROCm torch: libatomic.so.1). A GPU venv on a host with that kind of GPU
        must also reach it: a driver older than the wheel's CUDA imports fine and then runs every job on nothing."""
        art = self.env.get("COMFY_LAUNCH_ARTIFACTS", "")
        probe = ("import importlib.util, sys; v = '%d.%d.%d' % sys.version_info[:3]\n"
                 "if importlib.util.find_spec('torch'):\n"
                 " import torch; v += ' · torch ' + torch.__version__\n"
                 " if sys.argv[1:] == ['gpu']:\n"
                 "  try:\n"
                 "   torch.cuda.init(); n = torch.cuda.device_count()\n"
                 "   v += ' · %d GPU(s): %s' % (n, torch.cuda.get_device_name(0))\n"
                 "  except Exception as e:\n"
                 "   rt = torch.version.cuda and 'CUDA ' + torch.version.cuda or torch.version.hip and 'HIP ' + torch.version.hip or '?'\n"
                 "   print('comfyfarm-gpu-error: %s (torch built for %s)' % ((str(e).strip().splitlines() or [type(e).__name__])[0], rt))\n"
                 "print('comfyfarm-venv:', v)")                       # marked: GPU runtimes print noise of their own
        reach = {"rocm": "amd", "cuda": "nvidia"}
        usable = {"amd": {"rocm", "cpu", None}, "nvidia": {"cuda", "cpu", None}, "none": {"cpu", None}}.get(self.cfg.get("gpu", "any"))
        for ready in sorted(glob.glob(os.path.join(art, "venv", "*", "READY"))) if art else []:
            v = os.path.dirname(ready)
            name = os.path.basename(v)
            try:
                with open(os.path.join(v, "manifest.json")) as f:
                    accel = json.load(f).get("accelerator")
            except (OSError, ValueError):
                accel = None
            if usable is not None and accel not in usable:
                continue                                 # a ROCm venv on a CPU-only UI host, a CUDA venv on an AMD host
            py = os.path.join(v, "bin", "python")
            gpu = reach.get(accel) in getattr(self, "found", [])
            code, out = self.cmd([py, "-c", probe] + (["gpu"] if gpu else []), timeout=300)
            mark = re.search(r"comfyfarm-venv: (.+)", out)
            if code == 0 and (bad := re.search(r"comfyfarm-gpu-error: (.+)", out)):
                self.add(FAIL, f"venv {name}", f"torch cannot use this host's GPU: {bad.group(1).strip()[:300]}"
                         + (" — the NVIDIA driver must be new enough for that CUDA (nvidia-smi shows the highest it "
                            "supports): update the driver, or use a recipe built for an older CUDA" if accel == "cuda" else ""))
                continue
            if code == 0 and mark:
                self.add(OK, f"venv {name}", f"Python {mark.group(1).strip()} runs here")
                continue
            lib = re.search(r"(lib[\w.+-]+\.so[.\d]*): cannot open shared object file", out)
            if lib:
                pkg = SYSTEM_LIBS.get(lib.group(1))
                self.add(FAIL, f"venv {name}", f"a system library is missing on this host: {lib.group(1)}"
                         + (f" — install {pkg} from your local mirror" if pkg else " — install the package that provides it"))
            elif not os.path.exists(os.path.realpath(py)):
                self.add(FAIL, f"venv {name}", f"its interpreter is not on this host ({os.path.realpath(py)}) — a venv "
                         "built before D-42 points into the build host; publish it again")
            else:
                self.add(FAIL, f"venv {name}", f"does not run here: {out.strip()[-200:] or code}")

    # ---- time and network -----------------------------------------------------------------------------------------
    def time_and_network(self):
        code, out = self.cmd(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
        sync = out.strip() == "yes"
        self.add(OK if sync else WARN, "clock", "NTP synchronised" if sync else
                 "not synchronised — job times and session expiry compare clocks across hosts")
        router = self.env.get("COMFY_LAUNCH_ROUTER", "").rstrip("/")
        try:
            t0 = time.time()
            with urllib.request.urlopen(f"{router}/api/health/dispatch", timeout=5) as r:
                r.read(1)
                date = r.headers.get("Date")
            skew = ""
            if date:
                from email.utils import parsedate_to_datetime
                d = abs(parsedate_to_datetime(date).timestamp() - t0)
                skew = f", clocks {d:.0f} s apart" if d >= 5 else ""
                if d >= 30:
                    self.add(WARN, "clock", f"{d:.0f} s off the router's clock")
            self.add(OK, "router", f"{router} answers{skew}")
        except Exception as e:                                           # noqa: BLE001 — any failure is the finding
            self.add(FAIL, "router", f"{router or '(not set)'} does not answer: {type(e).__name__} — firewall, address, "
                     "or the router is down")
        port, listen = self.cfg.get("port"), self.cfg.get("listen", "0.0.0.0")
        if port:
            s = socket.socket()
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((listen, int(port)))
                self.add(OK, "port", f"{listen}:{port} is free")
            except OSError:
                if self.cfg.get("container"):    # in its own network namespace only this instance listens there
                    mine = True
                else:
                    code, out = self.cmd(["systemctl", "--user", "is-active", f"comfyfarm@{self.cfg.get('instance', '')}"])
                    mine = out.strip() == "active"
                self.add(OK if mine else FAIL, "port", f"{listen}:{port} is in use" + (" by this instance" if mine else
                         " by something else — pick another port for the instance"))
            finally:
                s.close()

    def groups(self):
        if self.cfg.get("container"):
            return
        names = {g.gr_name for g in grp.getgrall() if self.user in g.gr_mem}
        try:
            names.add(grp.getgrgid(pwd.getpwnam(self.user).pw_gid).gr_name)
        except KeyError:
            pass
        want = [g for g in ("render", "video") if _group_exists(g)]
        missing = [g for g in want if g not in names]
        if want:
            self.add(OK if not missing else WARN, "groups", f"{self.user}: {', '.join(sorted(names & set(want))) or 'none'}"
                     + (f" — missing {', '.join(missing)}" if missing else ""))

    def run(self) -> list[dict]:
        for step in (self.system, self.groups, self.gpu, self.paths, self.venvs, self.builds, self.time_and_network):
            try:
                step()
            except Exception as e:                                       # noqa: BLE001 — one broken check is a finding
                self.add(FAIL, step.__name__, f"check failed: {type(e).__name__}: {e}")
        return self.out


def _group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    if len(args) != 1 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", args[0]):
        print("usage: hostcheck.py <instance> [--json]", file=sys.stderr)
        return 2
    inst = args[0]
    os.chdir("/")                    # a working directory the account cannot read makes ROCm abort (rehearsal finding)
    try:
        env = read_env(os.path.join(ETC, f"{inst}.env"))
        with open(os.path.join(ETC, f"{inst}.check.json")) as f:
            cfg = json.load(f)
    except OSError as e:
        print(f"hostcheck: instance {inst} is not set up here ({e})", file=sys.stderr)
        return 2
    out = Check(env, dict(cfg, instance=inst)).run()
    if as_json:
        print(json.dumps(out))
    else:
        for r in out:
            print(f"{r['level']:<5}{r['name']:<18}{r['detail']}")
    return 1 if any(r["level"] == FAIL for r in out) else 0


if __name__ == "__main__":
    sys.exit(main())
