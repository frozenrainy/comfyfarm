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
               "libtinfo.so.6": "libtinfo6", "libstdc++.so.6": "libstdc++6", "libquadmath.so.0": "libquadmath0",
               # what node packs' wheels load (pymeshlab, opencv's GUI builds, open3d …): a minimal server lacks them
               "libGL.so.1": "libgl1", "libOpenGL.so.0": "libopengl0", "libEGL.so.1": "libegl1", "libX11.so.6": "libx11-6",
               "libxcb.so.1": "libxcb1", "libXext.so.6": "libxext6", "libXrender.so.1": "libxrender1", "libSM.so.6": "libsm6",
               "libICE.so.6": "libice6", "libglib-2.0.so.0": "libglib2.0-0t64", "libgthread-2.0.so.0": "libglib2.0-0t64",
               "libfontconfig.so.1": "libfontconfig1", "libfreetype.so.6": "libfreetype6", "libcom_err.so.2": "libcom-err2",
               "libgpg-error.so.0": "libgpg-error0", "libp11-kit.so.0": "libp11-kit0", "libsndfile.so.1": "libsndfile1"}
MISSING_LIB = re.compile(r"(lib[\w.+-]+\.so[.\d]*): cannot open shared object file")


def explain(error: str) -> str:
    """An import error, with the package to install when it is a missing system library."""
    m = MISSING_LIB.search(error or "")
    if not m:
        return error
    pkg = SYSTEM_LIBS.get(m.group(1))
    return f"{m.group(1)} is missing on the host" + (f" — install {pkg}" if pkg else " — install the package that provides it")


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


def has_models(path: str, depth: int = 3, budget: int = 5000) -> bool:
    """Any file with content within a few levels (ComfyUI's own tree has empty put_…_here placeholders)."""
    todo = [(path, 0)]
    while todo and budget > 0:
        d, level = todo.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            budget -= 1
            if e.name.startswith("."):
                continue
            try:
                if e.is_file() and e.stat().st_size > 0:
                    return True
                if e.is_dir() and level + 1 < depth:
                    todo.append((e.path, level + 1))
            except OSError:
                continue
    return False


class Msg(str):
    """A line of the check in English that also keeps how it was made — the template and its values — so the dashboard
    can show it in the reader's language (the template is the key, as in the dashboard's own texts). Templates are whole
    sentences: no pieces glued together, which no translation could follow."""

    def __new__(cls, template: str, **args):
        m = super().__new__(cls, template.format(**args))
        m.template, m.args = template, {k: str(v) for k, v in args.items()}
        return m


class Check:
    """`root` prefixes the device and system paths (/dev, /sys, /var/lib/systemd) so tests can build a fake host."""

    def __init__(self, env: dict, cfg: dict, *, root: str = "", cmd=run_cmd, user: str | None = None):
        self.env, self.cfg, self.root, self.cmd = env, cfg, root.rstrip("/"), cmd
        if not user:
            try:
                user = pwd.getpwuid(os.getuid()).pw_name
            except KeyError:                    # a container's uid without an /etc/passwd entry (the UI worker)
                user = os.environ.get("USER") or f"uid {os.getuid()}"
        self.user = user
        self.out: list[dict] = []

    def add(self, level: str, name: str, detail: str, **data):
        row = {"level": level, "name": name, "detail": str(detail), **data}
        if isinstance(detail, Msg):
            row.update(t=detail.template, a=detail.args)
        self.out.append(row)

    def p(self, path: str) -> str:
        return self.root + path

    # ---- system ---------------------------------------------------------------------------------------------------
    def system(self):
        v = sys.version_info
        version, need = f"{v[0]}.{v[1]}.{v[2]}", f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
        if v[:2] >= MIN_PYTHON:
            self.add(OK, "python", Msg("{version} for the launcher", version=version))
        else:
            self.add(FAIL, "python", Msg("{version} for the launcher — needs {need} or newer", version=version, need=need))
        if self.cfg.get("container"):        # the UI worker container: its supervisor is the service manager
            return
        linger = os.path.exists(self.p(f"/var/lib/systemd/linger/{self.user}"))
        if linger:
            self.add(OK, "lingering", Msg("{user}: on — the instance starts at boot", user=self.user))
        else:
            self.add(FAIL, "lingering", Msg("{user}: off — the user manager stops at logout (setup runs `loginctl "
                                            "enable-linger`)", user=self.user))
        code, out = self.cmd(["systemctl", "--user", "is-system-running"])
        state = out.splitlines()[-1] if out else ""
        self.add(OK if state in ("running", "degraded") else FAIL, "user manager",
                 Msg("systemd --user: {state}", state=state) if state else Msg("systemd --user: not reachable"))

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
                    ver = f.read().strip()
            if usable:
                found.append("amd")
                gpus = dict(n=len(targets), targets=", ".join(targets) or "?")
                self.add(OK, "GPU (AMD)", Msg("{n} GPU(s): {targets} · amdgpu {version}", **gpus, version=ver) if ver
                         else Msg("{n} GPU(s): {targets}", **gpus))
            else:
                self.add(FAIL, "GPU (AMD)", Msg("/dev/kfd present but {user} cannot open it or a render node — add the "
                         "account to the render and video groups (setup does), then log in again", user=self.user))
        if os.path.exists(self.p("/dev/nvidiactl")):
            if not os.access(self.p("/dev/nvidiactl"), os.R_OK | os.W_OK):
                self.add(FAIL, "GPU (NVIDIA)", Msg("/dev/nvidiactl present but {user} cannot open it", user=self.user))
            else:
                code, out = self.cmd(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])
                if code == 0 and out:
                    found.append("nvidia")
                    self.add(OK, "GPU (NVIDIA)", "; ".join(out.splitlines()))
                else:
                    self.add(FAIL, "GPU (NVIDIA)", Msg("nvidia-smi failed: {error} — driver not loaded?", error=out[:200] or code))
        self.found = found
        if want in ("amd", "nvidia") and want not in found:
            self.add(FAIL, "GPU", Msg("setup expects an {kind} GPU and none is usable here (driver installed and loaded?)",
                                      kind=want.upper()))
        elif not found and want == "any":
            self.add(WARN, "GPU", Msg("no GPU found — this host can only run ComfyUI on the CPU"))

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
        self.add(OK if names else WARN, "builds",
                 Msg("from the dashboard: {recipes} (built as comfyfarm-build)", recipes=", ".join(names)) if names else
                 Msg("from the dashboard: allowed, but no recipe is installed — run setup again"), recipes=names)

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
            if usable:
                self.add(OK, "recipes", Msg("{templates} (templates: System → Rollout → From a recipe makes profiles of "
                                            "them)", templates=", ".join(usable)), templates=templates)
            else:
                self.add(WARN, "recipes", Msg("{templates}: their artifacts are missing — build again (System → Rollout → "
                         "Build)", templates=", ".join(t["template"] for t in templates)), templates=templates)
        else:
            self.add(WARN, "recipes", Msg("no profile template in {art} — on a host with internet that mounts it here: "
                     "python3 publish.py recipe {art} rocm-gfx1201 (docs/guide/rollouts.md §1)", art=art))

    # ---- paths ----------------------------------------------------------------------------------------------------
    def paths(self):
        art, local = self.env.get("COMFY_LAUNCH_ARTIFACTS", ""), self.env.get("COMFY_LAUNCH_LOCAL", "")
        published = len(glob.glob(os.path.join(art, "*", "*", "READY"))) if art else 0
        if not art or not os.path.isdir(art) or not os.access(art, os.R_OK | os.X_OK):
            self.add(FAIL, "artifact store", Msg("{path} is not readable — is the store mounted here, at the same path as "
                     "on the other hosts?", path=art or "(not set)"))
        else:
            if published:
                self.add(OK, "artifact store", Msg("{path}: {n} published artifact(s)", path=art, n=published))
            else:
                self.add(WARN, "artifact store", Msg("{path}: nothing published — publish ComfyUI, a venv and the node set "
                         "before assigning a profile", path=art))
            self.recipes(art)
        if not local or not os.path.isdir(local) or not os.access(local, os.W_OK):
            self.add(FAIL, "local root", Msg("{path} is not writable by {user}", path=local or "(not set)", user=self.user))
        else:
            free = shutil.disk_usage(local).free
            disk = dict(path=local, gb=f"{free / 2**30:.0f}")
            if free >= LOCAL_FREE_WARN:
                self.add(OK, "local root", Msg("{path}: {gb} GB free", **disk))
            else:
                self.add(WARN, "local root", Msg("{path}: {gb} GB free — less than {min} GB: copies of ComfyUI and the "
                                                 "node sets go here", **disk, min=LOCAL_FREE_WARN // 2**30))
        dirs = self.cfg.get("paths", {})
        for key in ("input", "output", "user", "temp"):
            d = dirs.get(key)
            if not d:
                continue
            ok = os.path.isdir(d) and os.access(d, os.W_OK | os.X_OK)
            who = f"{self.user} (uid {os.getuid()})" if not self.root else self.user
            if ok:
                self.add(OK, f"{key} directory", d)
            else:
                self.add(FAIL, f"{key} directory", Msg("{path} — missing or not writable by {who}; on shared storage give "
                         "that uid write access there (every host uses the same uid)", path=d, who=who))
        if dirs.get("models"):
            m = dirs["models"]
            if not (os.path.isdir(m) and os.access(m, os.R_OK | os.X_OK)):
                self.add(FAIL, "models", Msg("{path} — missing or not readable: mount the models there (the same on every "
                         "GPU worker) or make the directory and copy them in", path=m))
            elif self.cfg.get("role") == "ui" or self.cfg.get("gpu") == "none" or has_models(m):
                self.add(OK, "models", m)          # the UI worker needs none: the GPU workers' snapshot names them
            else:
                self.add(WARN, "models", Msg("{path} is empty — ComfyUI will list no models and every job that loads one "
                         "fails: put them in its subdirectories as in ComfyUI's own models directory (checkpoints/, "
                         "loras/, vae/, …)", path=m))
        try:
            try:
                import launcher                                          # on a worker: installed next to this file
            except ImportError:
                from comfyfarm import launcher                           # in the package (tests, the router's copy)
            code = [art] + [f"{local}/{d}" for d in launcher.MANAGED_LOCAL]
            for key in ("input", "output", "user", "temp"):
                d = dirs.get(key)
                if d and art and local and (launcher.under(d, code) or any(launcher.under(c, [d]) for c in code)):
                    self.add(FAIL, f"{key} directory", Msg("{path} overlaps <ART> or the code under <LOCAL> — the "
                             "launcher will refuse every render (contract §4)", path=d))
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
                err = bad.group(1).strip()[:300]
                self.add(FAIL, f"venv {name}", Msg("torch cannot use this host's GPU: {error} — the NVIDIA driver must be "
                         "new enough for that CUDA (nvidia-smi shows the highest it supports): update the driver, or use a "
                         "recipe built for an older CUDA", error=err) if accel == "cuda" else
                         Msg("torch cannot use this host's GPU: {error}", error=err))
                continue
            if code == 0 and mark:
                self.add(OK, f"venv {name}", Msg("Python {version} runs here", version=mark.group(1).strip()))
                continue
            lib = MISSING_LIB.search(out)
            if lib:
                pkg = SYSTEM_LIBS.get(lib.group(1))
                self.add(FAIL, f"venv {name}", Msg("a system library is missing on this host: {lib} — install {pkg} from "
                         "your local mirror", lib=lib.group(1), pkg=pkg) if pkg else
                         Msg("a system library is missing on this host: {lib} — install the package that provides it",
                             lib=lib.group(1)))
            elif not os.path.exists(os.path.realpath(py)):
                self.add(FAIL, f"venv {name}", Msg("its interpreter is not on this host ({path}) — a venv built before "
                         "D-42 points into the build host; publish it again", path=os.path.realpath(py)))
            else:
                self.add(FAIL, f"venv {name}", Msg("does not run here: {error}", error=out.strip()[-200:] or code))

    # ---- time and network -----------------------------------------------------------------------------------------
    def time_and_network(self):
        if self.cfg.get("container"):          # a container has the host's clock and no timedatectl of its own
            self.add(OK, "clock", Msg("the host's (a container keeps it); compared with the router's below"))
        else:
            code, out = self.cmd(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
            sync = out.strip() == "yes"
            if sync:
                self.add(OK, "clock", Msg("NTP synchronised"))
            else:
                self.add(WARN, "clock", Msg("not synchronised — job times and session expiry compare clocks across hosts"))
        router = self.env.get("COMFY_LAUNCH_ROUTER", "").rstrip("/")
        try:
            t0 = time.time()
            with urllib.request.urlopen(f"{router}/api/health/dispatch", timeout=5) as r:
                r.read(1)
                date = r.headers.get("Date")
            d = 0.0
            if date:
                from email.utils import parsedate_to_datetime
                d = abs(parsedate_to_datetime(date).timestamp() - t0)
                if d >= 30:
                    self.add(WARN, "clock", Msg("{seconds} s off the router's clock", seconds=f"{d:.0f}"))
            if d >= 5:
                self.add(OK, "router", Msg("{router} answers, clocks {seconds} s apart", router=router, seconds=f"{d:.0f}"))
            else:
                self.add(OK, "router", Msg("{router} answers", router=router))
        except Exception as e:                                           # noqa: BLE001 — any failure is the finding
            self.add(FAIL, "router", Msg("{router} does not answer: {error} — firewall, address, or the router is down",
                                         router=router or "(not set)", error=type(e).__name__))
        listen, port = self.running_address()
        if port:
            s = socket.socket()
            addr = f"{listen}:{port}"
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((listen, int(port)))
                self.add(OK, "port", Msg("{addr} is free", addr=addr))
            except OSError:
                if self.cfg.get("container"):    # in its own network namespace only this instance listens there
                    mine = True
                else:
                    code, out = self.cmd(["systemctl", "--user", "is-active", f"comfyfarm@{self.cfg.get('instance', '')}"])
                    mine = out.strip() == "active"
                if mine:
                    self.add(OK, "port", Msg("{addr} is in use by this instance", addr=addr))
                else:
                    self.add(FAIL, "port", Msg("{addr} is in use by something else — pick another port for the instance",
                                               addr=addr))
            finally:
                s.close()

    def running_address(self) -> tuple[str, int | None]:
        """Where the instance listens: the render it last started with (the launcher saves it under <LOCAL>), which
        follows a later change of the instance (System → Rollout → Configure); else what setup was asked for."""
        listen, port = self.cfg.get("listen", "0.0.0.0"), self.cfg.get("port")
        saved = os.path.join(self.env.get("COMFY_LAUNCH_LOCAL", ""), "run", self.cfg.get("instance", ""), "render.json")
        try:
            with open(saved) as f:
                argv = json.load(f).get("argv") or []
            for i, a in enumerate(argv[:-1]):
                if a == "--listen":
                    listen = str(argv[i + 1])
                elif a == "--port":
                    port = int(argv[i + 1])
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return listen, port

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
            has = ", ".join(sorted(names & set(want))) or "-"
            if missing:
                self.add(WARN, "groups", Msg("{user}: {groups} — missing {missing}", user=self.user, groups=has,
                                             missing=", ".join(missing)))
            else:
                self.add(OK, "groups", Msg("{user}: {groups}", user=self.user, groups=has))

    def run(self) -> list[dict]:
        for step in (self.system, self.groups, self.gpu, self.paths, self.venvs, self.builds, self.time_and_network):
            try:
                step()
            except Exception as e:                                       # noqa: BLE001 — one broken check is a finding
                self.add(FAIL, step.__name__, Msg("check failed: {error}", error=f"{type(e).__name__}: {e}"))
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
