"""Publish artifacts into the store (contract §2) — a minimal stand-in for the builder (§8), without its gates:

    python3 publish.py core     <ART> <name> --from <unpacked ComfyUI release>
    python3 publish.py nodes    <ART> <name> --from <dir of custom node packages>
    python3 publish.py frontend <ART> <name> --from <built frontend dist>
    python3 publish.py venv     <ART> <name> --python python3.12 -r requirements.txt [--accelerator rocm --arch gfx1201]
                                [--pip-arg=--index-url=…] [--uv /path/to/uv]   (uv: same rules, its wheel cache)
    python3 publish.py recipe   <ART> <recipe> [--uv /path/to/uv] [--rebuild]
                                everything a farm needs from one recipe (recipes/<recipe>.json): the ComfyUI release, the
                                GPU and UI node sets, their venvs, and a profile template the dashboard turns into profiles

Rules kept: a name is never reused (an existing directory is refused); READY is written last, only after the copy or
build and the checks, and the artifact is made read-only first; a venv is built at its final path (absolute paths are
baked in) and must not reference anything outside itself (.pth files pointing elsewhere, editable installs); a core
has no symlinks (the release archive as is) and a node or frontend symlink stays inside its artifact. Standard
library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

try:
    from .launcher import CONTRACT, NAME, OPTIONS, env_problem, tree_sha256
except ImportError:                                        # run as a copied single file next to launcher.py
    from launcher import CONTRACT, NAME, OPTIONS, env_problem, tree_sha256     # type: ignore


class Refused(Exception):
    pass


def count(root: str) -> tuple[int, int]:
    files = size = 0
    for d, _, names in os.walk(root):
        for n in names:
            p = os.path.join(d, n)
            if not os.path.islink(p):
                files += 1
                size += os.path.getsize(p)
    return files, size


def content_sha256(root: str) -> str:
    """SHA-256 over every file's contents and every link's target, in path order — the publishing side's notion of
    "the same tree". Contract §2's tree_sha256 is path+size only (a change-detection key), so two different commits
    whose files keep their sizes would look alike by it."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for n in sorted(filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]):
            p = os.path.join(dirpath, n)
            rel = os.path.relpath(p, root)
            if rel in ("READY", "manifest.json"):
                continue
            h.update(rel.encode("utf-8", "surrogateescape") + b"\0")
            if os.path.islink(p):
                h.update(b"l" + os.readlink(p).encode("utf-8", "surrogateescape") + b"\0")
                continue
            h.update(b"f")
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            h.update(b"\0")
    return h.hexdigest()


def write_manifest(path: str, kind: str, name: str, **extra) -> dict:
    files, size = count(path)
    try:
        digest = tree_sha256(path)
    except ValueError as e:                          # contract §2: unsupported entry kinds / newline names
        raise Refused(str(e))
    m = {"contract": CONTRACT, "kind": kind, "name": name, "created": dt.datetime.now(dt.timezone.utc).isoformat(),
         "build_host": socket.gethostname(), "files": files, "bytes": size, "tree_sha256": digest, **extra}
    with open(os.path.join(path, "manifest.json"), "w") as f:
        json.dump(m, f, indent=1)
    return m


def target(art: str, kind: str, name: str) -> str:
    if not NAME.match(name):
        raise Refused(f"bad name {name!r}")
    path = os.path.join(os.path.abspath(art), kind, name)
    if os.path.exists(path):
        raise Refused(f"{kind}/{name} exists: a published name is never reused")
    return path


def publish_tree(art: str, kind: str, name: str, src: str, **extra) -> dict:
    """extra: additive manifest fields (the contract ignores unknown ones) — content_sha256 is always added."""
    path = target(art, kind, name)
    if kind == "core" and not os.path.isfile(os.path.join(src, "main.py")):
        raise Refused(f"{src} does not look like a ComfyUI release (no main.py)")
    try:
        tree_sha256(src)                                  # refuse odd entries before copying anything (contract §2)
    except ValueError as e:
        raise Refused(str(e))
    tmp = f"{path}.tmp-{os.getpid()}"
    shutil.copytree(src, tmp, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    if bad := symlink_problems(tmp, kind):
        shutil.rmtree(tmp, ignore_errors=True)
        raise Refused("; ".join(bad[:5]) + (f" (+{len(bad) - 5} more)" if len(bad) > 5 else ""))
    os.rename(tmp, path)
    m = write_manifest(path, kind, name, content_sha256=content_sha256(path), **extra)
    seal(path)
    return m


def symlink_problems(root: str, kind: str) -> list[str]:
    """Contract §2: a core is the release archive as is — no symlinks. Elsewhere a symlink must stay inside the
    artifact: relative, and resolving below its root (an absolute or escaping link reaches code nobody published)."""
    bad, top = [], os.path.realpath(root)
    for d, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(d, n)
            if not os.path.islink(p):
                continue
            rel, target = os.path.relpath(p, root), os.readlink(p)
            real = os.path.realpath(p)
            if kind == "core":
                bad.append(f"symlink in a core: {rel}")
            elif os.path.isabs(target) or not (real == top or real.startswith(top + os.sep)):
                bad.append(f"symlink points outside the artifact: {rel} -> {target}")
    return bad


def seal(path: str) -> None:
    """Published artifacts are read-only (contract §2): everything below is made read-only, then READY is written and
    the top directory locked too."""
    for d, dirs, files in os.walk(path, topdown=False):
        for n in dirs + files:
            p = os.path.join(d, n)
            if not os.path.islink(p):
                os.chmod(p, os.lstat(p).st_mode & ~0o222)
    ready = os.path.join(path, "READY")
    open(ready, "w").close()
    for p in (ready, path):
        os.chmod(p, os.lstat(p).st_mode & ~0o222)


def venv_escapes(path: str) -> list[str]:
    """.pth lines and editable installs that point outside the venv."""
    bad = []
    for d, _, names in os.walk(path):
        for n in names:
            if n.endswith(".pth"):
                with open(os.path.join(d, n), errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith(("#", "import")):
                            continue
                        full = os.path.normpath(os.path.join(d, line))     # relative lines: from the .pth's directory
                        if not full.startswith(os.path.normpath(path) + os.sep):
                            bad.append(f"{n}: {line}")
            if n == "direct_url.json":
                with open(os.path.join(d, n)) as f:
                    if json.load(f).get("dir_info", {}).get("editable"):
                        bad.append(f"editable install: {os.path.basename(d)}")
    return bad


def interpreter_problems(path: str) -> list[str]:
    """D-42: `bin/python` and pyvenv.cfg's `home` resolve inside the venv, or to the operating system's own Python
    under /usr (present at the same path on every host). Anything else exists only on the build host."""
    top, bad = os.path.realpath(path), []

    def ok(p: str) -> bool:
        return p == top or p.startswith(top + os.sep) or p.startswith("/usr/")
    real = os.path.realpath(os.path.join(path, "bin", "python"))
    if not ok(real):
        bad.append(f"bin/python resolves outside the venv: {real}")
    cfg = os.path.join(path, "pyvenv.cfg")
    if os.path.isfile(cfg):
        with open(cfg) as f:
            home = next((line.split("=", 1)[1].strip() for line in f if line.split("=", 1)[0].strip() == "home"), None)
        if home and not ok(os.path.realpath(home)):
            bad.append(f"pyvenv.cfg home is outside the venv: {home}")
    return bad


def carry_interpreter(python: str, path: str) -> str:
    """The interpreter the venv will run, inside the artifact (D-42). A self-contained build (uv's managed Pythons are
    python-build-standalone, made to be moved) is copied to <venv>/.python/; the operating system's own Python under
    /usr is used where it is. Returns the interpreter to build the venv with."""
    real = os.path.realpath(python)
    if real.startswith("/usr/"):
        return python
    root = os.path.dirname(os.path.dirname(real))                    # <install>/bin/python3.x → <install>
    dest = os.path.join(path, ".python", os.path.basename(root))
    shutil.copytree(root, dest, symlinks=True)
    return os.path.join(dest, "bin", os.path.basename(real))


def removed_by(overrides: list[str]) -> set[str]:
    """The packages an override file removes (the NEVER marker)."""
    out = set()
    for o in overrides:
        with open(o) as f:
            for line in f:
                if NEVER in line:
                    out.add(re.split(r"[;<>=!~\[ ]", line.strip(), maxsplit=1)[0].lower())
    return out


def check_dependencies(argv: list[str], overrides: list[str]) -> None:
    """pip/uv check, read rather than trusted blindly: a package asking for an OpenCV variant the overrides removed is
    fine (the kept one provides cv2), and a wheel whose metadata names another platform but runs here is only worth a
    warning (decord's says cp36). Anything else missing or incompatible stops the build."""
    p = subprocess.run(argv, capture_output=True, text=True)
    if p.returncode == 0:
        return
    removed, fatal = removed_by(overrides), []
    for line in (p.stdout + p.stderr).splitlines():
        m = (re.search(r"requires `([A-Za-z0-9._-]+)[^`]*`, but it's not installed", line)
             or re.search(r"requires ([A-Za-z0-9._-]+)[^,]*, which is not installed", line))
        if m and m.group(1).lower() in removed:
            continue
        if "built for a different platform" in line or "is not supported on this platform" in line:
            print(f"publish: warning: {line.strip()}", file=sys.stderr)
            continue
        if re.search(r"requires|has requirement|incompatib|not installed", line) and not line.startswith(("Found ", "Checked ")):
            fatal.append(line.strip())
    if fatal:
        raise Refused("dependency check failed:\n  " + "\n  ".join(fatal))


def publish_venv(art: str, name: str, python: str, requirements: list[str], accelerator: str | None,
                 arch: list[str], pip_args: list[str], uv: str | None = None, manifest_extra: dict | None = None,
                 constraints: list[str] | None = None, overrides: list[str] | None = None) -> dict:
    """Built at its final path, with its interpreter inside it (D-42), so every host that mounts the store at the same
    path can run it — not only the build host. All requirement files go into one install, so they are resolved
    together: one after the other, a later file could quietly change what an earlier one needed."""
    if overrides and not uv:
        raise Refused("overrides (one OpenCV, one onnxruntime) need uv — install it, or pass --uv")
    path = target(art, "venv", name)
    if os.sep not in python:                     # a version (3.12) or a command name, not a path
        found = (subprocess.run([uv, "python", "find", python], capture_output=True, text=True).stdout.strip() if uv
                 else shutil.which(python))
        if not found:
            raise Refused(f"no Python {python!r} found" + (" (uv python install it first)" if uv else ""))
        python = found
    py = os.path.join(path, "bin", "python")
    pip = [uv, "pip"] if uv else [py, "-m", "pip"]
    target_py = ["--python", py] if uv else []
    os.makedirs(path)
    base = carry_interpreter(python, path)
    if uv:
        subprocess.run([uv, "venv", "--allow-existing", "--python", base, path], check=True)   # at its final path
    else:
        subprocess.run([base, "-m", "venv", path], check=True)                                 # at its final path
    try:
        if bad := interpreter_problems(path):
            raise Refused("; ".join(bad))
        cmd = pip + ["install"] + target_py
        for r in requirements:
            cmd += ["-r", r]
        for c in constraints or []:
            cmd += ["-c", c]
        for o in overrides or []:
            cmd += ["--override", o]
        if requirements:
            subprocess.run(cmd + pip_args, check=True)
        check_dependencies(pip + ["check"] + target_py, overrides or [])
        if bad := venv_escapes(path):
            raise Refused("the venv references things outside itself: " + "; ".join(bad))
        freeze = subprocess.run(pip + ["freeze"] + target_py, check=True, capture_output=True, text=True).stdout
    except (subprocess.CalledProcessError, Refused):
        print(f"publish: venv/{name} failed and stays unpublished (no READY); remove it or use another name", file=sys.stderr)
        raise
    with open(os.path.join(path, "freeze.txt"), "w") as f:
        f.write(freeze)
    ver = subprocess.run([py, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"], capture_output=True, text=True).stdout.strip()
    extra = {"python": ver, "platform": "windows" if platform.system() == "Windows" else "posix"}
    if accelerator:
        extra["accelerator"] = accelerator             # contract §9.1; absent = unknown
    if arch:
        extra["arch"] = arch
    m = write_manifest(path, "venv", name, **extra, **(manifest_extra or {}))
    seal(path)
    return m


# ---- recipes: everything a farm needs, from one file ----------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
PYPI = "https://pypi.org/simple"
ROLES = ("gpu", "ui")
# What a farm venv always has besides ComfyUI's own requirements: torchaudio is not in them, yet node packs import it
# without listing it (they assume the usual torch/torchvision/torchaudio install). Unpinned: its last release imports
# fine on newer torch.
BASE_EXTRA = ["torchaudio"]
# Node packs ask for torch with their own idea of it; the venv's torch comes from the accelerator's index and stays.
TORCH_FAMILY = re.compile(r"^(torch|torchvision|torchaudio)\s*($|[<>=!~;\[(])", re.I)
# One cv2 and one onnxruntime: their variants install into the same module, and whichever installs last wins —
# packs ask for different ones directly and through their own dependencies. The kept OpenCV has every module and
# none of the desktop libraries a server lacks (libGL).
OPENCV = ("opencv-python", "opencv-python-headless", "opencv-contrib-python", "opencv-contrib-python-headless")
KEEP_OPENCV = "opencv-contrib-python-headless"
ONNXRUNTIME = ("onnxruntime", "onnxruntime-gpu")
NEVER = "; python_version < '0'"                            # a uv override with this marker removes the package
REQ_FILE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9_][A-Za-z0-9._/-]*\.txt$")   # a file inside the pack
SPEC = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._ -]+\])?\s*([<>=!~][<>=!~0-9A-Za-z.*+, ]*)?"
                  r"(;[A-Za-z0-9_ .'\"<>=!~()-]+)?\Z")             # name[extras] specifiers ; marker


def find_dir(name: str, given: str | None) -> str:
    """recipes/, worker_ext/, ui_ext/: given, next to this file (copied onto a host), or the repository root."""
    for d in ([given] if given else []) + [HERE, os.path.join(HERE, "..", "..")]:
        if os.path.isdir(os.path.join(d, name)):
            return os.path.abspath(d)
    raise Refused(f"no {name}/ next to publish.py or in the repository — run it from a ComfyFarm checkout")


REPO = re.compile(r"^(https://[A-Za-z0-9][A-Za-z0-9._~:/@-]*|/[A-Za-z0-9._][A-Za-z0-9._/-]*)$")   # https, or a local path
RELEASE = re.compile(r"^(v?[0-9][A-Za-z0-9._-]*|[0-9a-f]{40})$")                                     # a tag or a commit
COMMIT = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
UNPACKED_MAX, ENTRIES_MAX = 4 << 30, 100_000            # an archive pack that unpacks to more is refused (design 18)
INDEX = re.compile(r"^https://[A-Za-z0-9][A-Za-z0-9._~:/@%-]*$")


def load_recipe(name: str, recipes: str | None = None) -> dict:
    """A recipe is as trusted as code (root installs it on build hosts), so everything in it that reaches git, pip or
    a profile is checked here against the same rules the launcher applies — a typo surfaces now, not after the build.
    `name` is a file only when given as a path."""
    path = name if os.sep in name else os.path.join(find_dir("recipes", recipes), "recipes", name + ".json")
    try:
        with open(path) as f:
            r = json.load(f)
    except (OSError, ValueError) as e:
        raise Refused(f"recipe {name}: {e}")
    if not isinstance(r, dict) or not NAME.match(str(r.get("recipe", ""))):
        raise Refused(f"recipe {path}: 'recipe' must be a plain name")
    return check_recipe(r)


def check_recipe(r: dict) -> dict:
    who = f"recipe {r['recipe']}"
    c = r.get("comfyui") or {}
    if not isinstance(c, dict) or not REPO.match(str(c.get("repo", ""))) or not RELEASE.match(str(c.get("ref", ""))):
        raise Refused(f"{who}: comfyui needs repo (https://… or a local path) and ref (a release tag or a full commit, "
                      "never a branch: a recipe is pinned)")
    if not re.fullmatch(r"3\.\d{1,2}", str(r.get("python", "3.12"))):
        raise Refused(f"{who}: python must be a minor version like 3.12")
    for role in ROLES:
        v = r.get(role) or {}
        if (not isinstance(v, dict) or not NAME.match(str(v.get("venv", ""))) or v.get("accelerator") not in ("cuda", "rocm", "cpu")
                or not INDEX.match(str(v.get("index", "")))):
            raise Refused(f"{who}: {role} needs venv (a name), accelerator and index (an https URL)")
        if not all(isinstance(a, str) and NAME.match(a) for a in v.get("arch", [])):
            raise Refused(f"{who}: {role} arch must be plain names")
        if not all(isinstance(a, str) and a.startswith("--") and "\n" not in a for a in v.get("pip", [])):
            raise Refused(f"{who}: {role} pip entries must be options (--…)")
        # the torch family pinned by the recipe, extras and all — an index that ships each GPU's kernels as an extra
        # (AMD's ROCm 10: torch[device-gfx1201]) needs it, and a loose torchvision could come from PyPI built for CUDA
        if not isinstance(v.get("torch", []), list) or not all(
                isinstance(x, str) and "\n" not in x and TORCH_FAMILY.match(x) for x in v.get("torch", [])):
            raise Refused(f"{who}: {role} torch must be requirement lines for torch, torchvision and torchaudio only")
        if bad := [f for f in v.get("flags", []) if not isinstance(f, str) or (f.startswith("--") and f not in OPTIONS)]:
            raise Refused(f"{who}: {role} flags the launcher refuses: {bad}")
        if bad := [why for k, x in (v.get("env") or {}).items() if (why := env_problem(str(k), x))]:
            raise Refused(f"{who}: {role} environment the launcher refuses: {'; '.join(bad)}")
    for n in r.get("nodes", []):
        archive = isinstance(n, dict) and "archive" in n
        if archive and (not NAME.match(str(n.get("name", ""))) or not os.path.isabs(str(n.get("archive", "")))
                        or not SHA256.match(str(n.get("archive_sha256", ""))) or "repo" in n or "commit" in n):
            raise Refused(f"{who}: an archive node needs name, the archive's absolute path and its archive_sha256")
        if not archive and (not isinstance(n, dict) or not NAME.match(str(n.get("name", ""))) or not REPO.match(str(n.get("repo", "")))
                            or not COMMIT.match(str(n.get("commit", "")))):
            raise Refused(f"{who}: a node needs name, repo (https://… or a local path) and a full commit (no branches: "
                          "a recipe is pinned)")
        if not all(isinstance(x, str) and "\n" not in x and not x.startswith("-") for x in n.get("requirements", [])):
            raise Refused(f"{who}: node {n['name']} requirements must be requirement lines, not options")
        if "requirements_file" in n and not all(REQ_FILE.match(f.strip()) for f in str(n["requirements_file"]).split(",")):
            raise Refused(f"{who}: node {n['name']} requirements_file must be .txt files inside the pack (comma-separated)")
        if not isinstance(n.get("ui", True), bool):
            raise Refused(f"{who}: node {n['name']} ui must be true or false")
    names = [n["name"] for n in r.get("nodes", [])]
    if len(set(names)) != len(names) or set(names) & {"comfyfarm_worker", "comfyfarm_ui"}:
        raise Refused(f"{who}: node names must be unique and not comfyfarm_worker or comfyfarm_ui")
    for x in r.get("constraints", []) + [c for n in r.get("nodes", []) for c in n.get("constraints", [])]:
        if not isinstance(x, str) or "\n" in x or not SPEC.match(x.strip()):
            raise Refused(f"{who}: constraints must be version specifiers like 'kornia<0.8', not {x!r}")
    return r


FETCH_TRIES = 3
FETCH_WAIT = 5.0


def git_net(argv: list[str], env: dict | None = None, tries: int | None = None) -> subprocess.CompletedProcess:
    """A git command that talks to a server, tried again after a pause when it fails: a build fetches dozens of
    repositories, and one dropped connection (a TLS read error, an early EOF) must not end it."""
    tries = tries or FETCH_TRIES
    for i in range(tries):
        p = subprocess.run(argv, env=env, capture_output=True, text=True)
        if p.returncode == 0:
            return p
        if i + 1 < tries:
            print(f"publish: {' '.join(argv[3:5])} failed ({(p.stderr.strip().splitlines() or ['?'])[-1][:200]}) — "
                  f"trying again", file=sys.stderr)
            time.sleep(FETCH_WAIT * (i + 1))
    raise subprocess.CalledProcessError(p.returncode, argv, p.stdout, p.stderr)


def fetch(repo: str, ref: str, dest: str, submodules: dict | None = None) -> tuple[str, str]:
    """The tree of `ref` in `repo` without .git — the same bytes every time — and the commit it resolved to. repo and
    ref come after --end-of-options: git never reads them as options. Git submodules come along at the commits the
    pack pins (a pack without them is incomplete and may try to download them when ComfyUI imports it — which fails on a
    farm without internet); their URLs may be https only, or local paths when the pack itself is a local path."""
    src = dest + ".git-src"
    subprocess.run(["git", "init", "-q", src], check=True)
    git_net(["git", "-C", src, "fetch", "-q", "--depth", "1", "--end-of-options", repo, ref])
    commit = subprocess.run(["git", "-C", src, "rev-parse", "FETCH_HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    if subprocess.run(["git", "-C", src, "cat-file", "-e", "FETCH_HEAD:.gitmodules"], capture_output=True).returncode != 0:
        os.makedirs(dest)
        archive = subprocess.run(["git", "-C", src, "archive", "FETCH_HEAD"], check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", dest], input=archive, check=True)
    else:
        env = {**os.environ, "GIT_ALLOW_PROTOCOL": "https" if repo.startswith("https://") else "https:file",
               "GIT_TERMINAL_PROMPT": "0"}
        subprocess.run(["git", "-C", src, "checkout", "-q", "--detach", "FETCH_HEAD"], check=True)
        sub = ["git", "-C", src, "submodule", "update", "--init", "--recursive"]
        try:
            git_net(sub + ["--depth", "1"], env, tries=1)
        except subprocess.CalledProcessError:
            git_net(sub, env)                                     # a pinned commit that is not a branch tip, or the net
        status = subprocess.run(["git", "-C", src, "submodule", "status", "--recursive"], check=True,
                                capture_output=True, text=True).stdout
        if submodules is not None:
            for line in status.splitlines():
                parts = line[1:].split()
                if len(parts) >= 2:
                    submodules[parts[1]] = parts[0]
        shutil.copytree(src, dest, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    shutil.rmtree(src)
    return dest, commit


def unpack(archive: str, sha256: str, dest: str) -> str:
    """A node pack from an archive (docs/design/18): the file must have the SHA-256 the pack is pinned to; zip or tar
    (gzip or not), told by its first bytes. Only regular files and directories come out — absolute paths, `..`, links
    and devices refuse the archive, .git is left out — and at most UNPACKED_MAX bytes in ENTRIES_MAX entries. A single
    top-level directory is the pack (GitHub's "Download ZIP")."""
    import tarfile
    import zipfile
    h = hashlib.sha256()
    with open(archive, "rb") as f:
        head = f.read(262)
        f.seek(0)
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != sha256:
        raise Refused(f"{os.path.basename(archive)} is not the archive the pack is pinned to (SHA-256 {h.hexdigest()[:12]}…, "
                      f"pinned {sha256[:12]}…)")

    def clean(name: str) -> str | None:
        parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
        if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name) or ".." in parts:
            raise Refused(f"the archive names a path outside itself: {name!r}")
        return None if not parts or ".git" in parts else "/".join(parts)

    entries = []                                   # (path, is_dir, size, read) with read() -> bytes for files
    if head.startswith(b"PK\x03\x04"):
        z = zipfile.ZipFile(archive)
        for i in z.infolist():
            mode = (i.external_attr >> 16) & 0o170000
            if mode and mode not in (0o100000, 0o040000):
                raise Refused(f"the archive holds a link or a special file: {i.filename!r} — only files and directories")
            if (p := clean(i.filename)) is not None:
                entries.append((p, i.is_dir(), i.file_size, (lambda i=i: z.open(i))))
    elif head.startswith(b"\x1f\x8b") or head[257:262] == b"ustar":
        t = tarfile.open(archive, "r:*")
        for m in t.getmembers():
            if not (m.isfile() or m.isdir()):
                raise Refused(f"the archive holds a link or a special file: {m.name!r} — only files and directories")
            if (p := clean(m.name)) is not None:
                entries.append((p, m.isdir(), m.size, (lambda m=m: t.extractfile(m))))
    else:
        raise Refused(f"{os.path.basename(archive)} is not a zip or tar archive")
    if len(entries) > ENTRIES_MAX or sum(e[2] for e in entries) > UNPACKED_MAX:
        raise Refused(f"the archive unpacks to more than {ENTRIES_MAX} entries or {UNPACKED_MAX >> 30} GiB")
    files = [e for e in entries if not e[1]]
    if not files:
        raise Refused("the archive holds no files")
    tops = {e[0].split("/", 1)[0] for e in entries}
    strip = next(iter(tops)) + "/" if len(tops) == 1 and all("/" in e[0] or e[1] for e in entries) else ""
    os.makedirs(dest)
    for path, is_dir, size, read in entries:
        rel = path[len(strip):] if strip and path.startswith(strip) else (path if not strip else "")
        if not rel:
            continue
        out = os.path.join(dest, rel)
        if is_dir:
            os.makedirs(out, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with read() as src, open(out, "wb") as f:
            shutil.copyfileobj(src, f, 1 << 20)
        os.chmod(out, 0o644)
    return dest


def archive_top(archive: str) -> str | None:
    """The single top-level directory an archive unpacks from (what unpack() strips), or None."""
    import tarfile
    import zipfile
    try:
        if zipfile.is_zipfile(archive):
            names = zipfile.ZipFile(archive).namelist()
        else:
            with tarfile.open(archive, "r:*") as t:
                names = t.getnames()
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        return None                                  # unpack() refuses it with the real reason
    parts = [[p for p in n.replace("\\", "/").split("/") if p not in ("", ".")] for n in names]
    tops = {p[0] for p in parts if p}
    return next(iter(tops)) if len(tops) == 1 and any(len(p) > 1 for p in parts) else None


def pack_origin(n: dict, top=archive_top) -> str | None:
    """What makes two node packs the same pack: the repository, or the folder an uploaded archive unpacks from."""
    if "archive" in n:
        t = top(n["archive"])
        return f"archive folder {t}/" if t else None
    repo = re.sub(r"(\.git)?/*$", "", re.sub(r"^[a-z]+://", "", n.get("repo", "").strip().lower()))
    return f"repository {repo}" if repo else None


def refuse_same_pack(nodes: list[dict], top=archive_top) -> None:
    """Two versions of one pack in a node set register the same nodes and the same routes — ComfyUI stops at start
    ("method HEAD is already registered") and every worker on the set restarts until the rollout gives up (10-28:
    stage_carry-bcb08ff and stage_carry-fb0af5f, the new version added as a second pack instead of an update)."""
    seen: dict[str, str] = {}
    for n in nodes:
        origin = pack_origin(n, top)
        if origin is None:
            continue
        if origin in seen:
            raise Refused(f"node packs {seen[origin]} and {n['name']} are the same pack ({origin}) — a node set holds one "
                          f"copy: update {seen[origin]} to the new version instead of adding a second pack")
        seen[origin] = n["name"]


def node_requirements(n: dict, tree: str) -> tuple[list[str], list[str]]:
    """The requirement lines of one node pack, and notes on what was left out. The recipe's own list wins; otherwise
    the pack's requirements_file (requirements.txt by default — some packs ship a lighter one), otherwise the
    dependencies in its pyproject.toml. Never from a pack: options (an index URL, -r, -e), local paths, torch."""
    name = n["name"]
    if "requirements" in n:
        raw, origin = list(n["requirements"]), "recipe"
    elif n.get("requirements_file"):                      # one or more of the pack's files (some keep them per node)
        raw, origin = [], n["requirements_file"]
        for f in (x.strip() for x in n["requirements_file"].split(",") if x.strip()):
            path = os.path.join(tree, f)
            if not (os.path.isfile(path) and os.path.realpath(path).startswith(os.path.realpath(tree) + os.sep)):
                raise Refused(f"node {name}: no {f} in the pack")
            with open(path, encoding="utf-8", errors="replace") as fh:
                raw += fh.read().splitlines()
    elif os.path.isfile(os.path.join(tree, "requirements.txt")):
        with open(os.path.join(tree, "requirements.txt"), encoding="utf-8", errors="replace") as fh:
            raw, origin = fh.read().splitlines(), "requirements.txt"
    else:
        raw, origin = pyproject_dependencies(tree), "pyproject.toml"
    lines, notes = [], []
    for line in raw:
        line = re.sub(r"(^|\s)#.*$", "", line).strip()
        if not line:
            continue
        if line.startswith("-"):
            notes.append(f"{name}: '{line}' in {origin} ignored — options never come from a pack")
        elif TORCH_FAMILY.match(line):
            notes.append(f"{name}: '{line}' in {origin} ignored — the venv keeps its own torch")
        elif re.search(r"(^|@\s*)(file:|\.{0,2}/)", line):
            notes.append(f"{name}: '{line}' in {origin} ignored — a local path")
        else:
            lines.append(line)
    return lines, notes


def pyproject_dependencies(tree: str) -> list[str]:
    path = os.path.join(tree, "pyproject.toml")
    if not os.path.isfile(path):
        return []
    try:
        import tomllib
        with open(path, "rb") as f:
            deps = tomllib.load(f).get("project", {}).get("dependencies", [])
        return [d for d in deps if isinstance(d, str)]
    except (ImportError, ValueError):                       # Python 3.10, or a broken file: no list from it
        return []


ONE_OPENCV = re.compile(r"^opencv-(contrib-)?python(-headless)?(?![A-Za-z0-9_.-])(.*)$", re.I)
ONE_ORT = re.compile(r"^onnxruntime(-gpu)?(?![A-Za-z0-9_.-])(.*)$", re.I)


def keep_ort(accelerator: str) -> str:
    return "onnxruntime-gpu" if accelerator == "cuda" else "onnxruntime"


def one_of(lines: list[str], accelerator: str) -> list[str]:
    """A pack's own OpenCV or onnxruntime line becomes the kept package, with the pack's version bounds (the variants
    share their version numbers). The overrides remove the others — so the kept one must be asked for."""
    out = []
    for line in lines:
        if m := ONE_OPENCV.match(line):
            out.append(KEEP_OPENCV + m.group(3))
        elif m := ONE_ORT.match(line):
            out.append(keep_ort(accelerator) + m.group(2))
        else:
            out.append(line)
    return out


def needed_through_dependencies(uv: str, requirements: list[str], constraints: list[str], pip_args: list[str],
                                python: str, accelerator: str) -> list[str]:
    """The kept OpenCV / onnxruntime when only a dependency of a pack asks for a variant (mediapipe asks for
    opencv-contrib-python): resolve once without the overrides and look. If that cannot be told, ask for both."""
    cmd = [uv, "pip", "compile", "-q", "--no-header", "--python-version", python]
    for r in requirements:
        cmd += ["-r", r]
    for c in constraints:
        cmd += ["-c", c]
    cmd += [a for a in pip_args if a.startswith(("--index-url", "--extra-index-url", "--index-strategy", "--python-platform"))]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return [KEEP_OPENCV, keep_ort(accelerator)]
    names = {re.split(r"[=<>!~ \[;]", line.strip(), maxsplit=1)[0].lower() for line in p.stdout.splitlines()
             if line.strip() and not line.lstrip().startswith("#")}
    return ([KEEP_OPENCV] if names & set(OPENCV) else []) + ([keep_ort(accelerator)] if names & set(ONNXRUNTIME) else [])


def overrides_for(accelerator: str) -> str:
    """uv overrides that keep one OpenCV and the onnxruntime that fits the accelerator."""
    drop = [p for p in OPENCV if p != KEEP_OPENCV] + [p for p in ONNXRUNTIME if p != keep_ort(accelerator)]
    return "".join(p + NEVER + "\n" for p in drop)


def ready(art: str, kind: str) -> dict[str, dict]:
    out = {}
    for m in glob.glob(os.path.join(art, kind, "*", "manifest.json")):
        d = os.path.dirname(m)
        if os.path.exists(os.path.join(d, "READY")):
            with open(m) as f:
                out[os.path.basename(d)] = json.load(f)
    return out


def next_name(art: str, kind: str, prefix: str) -> str:
    """prefix-<n>, one past every name taken — published or not: a name is never reused."""
    bases = [os.path.basename(d).split(".tmp-")[0].removesuffix(".json")
             for d in glob.glob(os.path.join(art, kind, prefix + "-*"))]
    taken = [int(m.group(1)) for b in bases if (m := re.fullmatch(re.escape(prefix) + r"-(\d+)", b))]
    return f"{prefix}-{max(taken, default=0) + 1}"


def tree_or_refuse(path: str) -> str:
    try:
        return tree_sha256(path)
    except ValueError as e:
        raise Refused(str(e))


def same_tree(art: str, kind: str, src: str, prefix: str | None = None) -> str | None:
    """A published artifact with the same contents (content_sha256; one published without it is never reused)."""
    tree_or_refuse(src)                                   # refuse odd entries early (contract §2)
    c = content_sha256(src)
    for name, m in sorted(ready(art, kind).items()):
        if (prefix is None or re.fullmatch(re.escape(prefix) + r"-\d+", name)) and m.get("content_sha256") == c:
            return name
    return None


def stage_nodes(work: str, role: str, ext: str, fetched: dict[str, str], recipe: dict) -> str:
    stage = os.path.join(work, "nodes-" + role)
    os.makedirs(stage)
    ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc")
    shutil.copytree(os.path.join(ext, "worker_ext", "comfyfarm_worker"), os.path.join(stage, "comfyfarm_worker"), ignore=ignore)
    if role == "ui":
        shutil.copytree(os.path.join(ext, "ui_ext", "comfyfarm_ui"), os.path.join(stage, "comfyfarm_ui"), ignore=ignore)
    for n in nodes_for(recipe, role):
        shutil.copytree(fetched[n["name"]], os.path.join(stage, n["name"]), symlinks=True, ignore=ignore)
    return stage


def nodes_for(recipe: dict, role: str) -> list[dict]:
    """GPU workers get every pack; the UI worker too (their web code, routes and node definitions), except packs marked
    "ui": false — those that only import with a GPU (the UI worker runs on the CPU)."""
    return [n for n in recipe.get("nodes", []) if role == "gpu" or n.get("ui", True)]


def venv_inputs(recipe: dict, role: str, requirements: list[str], extra: list[str] = ()) -> str:
    """What decides a venv's contents. A venv build is not byte-for-byte repeatable, so reuse goes by its inputs."""
    v = recipe[role]
    blob = {"python": recipe.get("python", "3.12"), "accelerator": v["accelerator"], "arch": v.get("arch", []),
            "index": v["index"], "pip": v.get("pip", []), "requirements": [open(r).read() for r in requirements],
            "constraints_overrides": [open(r).read() for r in extra]}
    return hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()


def publish_recipe(art: str, name: str, uv: str | None = None, rebuild: bool = False, recipes: str | None = None,
                   ext: str | None = None, log=print, roles: tuple[str, ...] = ROLES, extra_nodes: list[dict] | None = None,
                   platform: str | None = None) -> dict:
    """Publish what the recipe names, reusing what is already there with the same content, and write a profile
    template (<ART>/profiles/<template>.json, ComfyFarm's own — not part of the contract) for the dashboard."""
    recipe, ext = load_recipe(name, recipes), find_dir("worker_ext", ext)
    if extra_nodes:                                  # node packs picked in the dashboard (builder.py checked their source)
        recipe = check_recipe({**recipe, "nodes": recipe.get("nodes", []) + list(extra_nodes),
                               "about": (recipe.get("about", "") + f" + {len(extra_nodes)} node pack(s) from the dashboard").strip()})
    refuse_same_pack(recipe.get("nodes", []))         # before anything is fetched: the set could not start anyway
    roles = tuple(r for r in ROLES if r in roles)
    if not roles:
        raise Refused("roles: gpu, ui or both")
    if platform and not re.fullmatch(r"[a-z0-9_][a-z0-9_.-]*", platform):
        raise Refused(f"bad platform {platform!r}")
    target = [f"--python-platform={platform}"] if platform and uv else []     # wheels every worker host can load
    art = os.path.abspath(art)
    uv = uv or shutil.which("uv") or next((p for p in (os.path.expanduser("~/.local/bin/uv"),) if os.path.exists(p)), None)
    py = recipe.get("python", "3.12")
    if uv:
        subprocess.run([uv, "python", "install", py], check=True)
    with tempfile.TemporaryDirectory(prefix="comfyfarm-recipe-") as work:
        c = recipe["comfyui"]
        core_src, core_commit = fetch(c["repo"], c["ref"], os.path.join(work, "core"))
        core = "comfyui-" + (c["ref"].lstrip("v") if not COMMIT.match(c["ref"]) else c["ref"][:12])
        if not NAME.match(core):
            raise Refused(f"comfyui ref {c['ref']!r} does not make a name")
        if os.path.exists(os.path.join(art, "core", core)):
            have = ready(art, "core").get(core)
            same = have and (have.get("content_sha256") == content_sha256(core_src) if have.get("content_sha256")
                             else have.get("tree_sha256") == tree_or_refuse(core_src))     # published before content hashes
            if not same:
                raise Refused(f"core/{core} exists with other contents (or unpublished) — not the release {c['ref']} "
                              f"(commit {core_commit[:12]})")
            log(f"core/{core}: already published, reused")
        else:
            publish_tree(art, "core", core, core_src, source={"repo": c["repo"], "ref": c["ref"], "commit": core_commit})
            log(f"core/{core}: published (commit {core_commit[:12]})")
        fetched, commits, subs, reqs = {}, {}, {}, {}
        for n in recipe.get("nodes", []):
            subs[n["name"]] = {}
            if "archive" in n:                     # an uploaded archive, pinned by its SHA-256 (design 18)
                fetched[n["name"]] = unpack(n["archive"], n["archive_sha256"], os.path.join(work, "node-" + n["name"]))
                commits[n["name"]] = "archive:" + n["archive_sha256"]
            else:
                fetched[n["name"]], commits[n["name"]] = fetch(n["repo"], n["commit"], os.path.join(work, "node-" + n["name"]),
                                                               subs[n["name"]])
            reqs[n["name"]], notes = node_requirements(n, fetched[n["name"]])
            for note in notes:
                log(note)
            log(f"node {n['name']}: {len(reqs[n['name']])} requirement(s)"
                + (f", {len(subs[n['name']])} submodule(s)" if subs[n["name"]] else ""))
        base = os.path.join(work, "base-requirements.txt")
        with open(base, "w") as f:
            f.write("\n".join(BASE_EXTRA) + "\n")
        template = {"recipe": recipe["recipe"], "about": recipe.get("about", ""), "comfyui": c["ref"]}
        for role in roles:
            # each role's venv has the requirements of the packs that role runs, resolved together with ComfyUI's
            acc = recipe[role]["accelerator"]
            req_files = [os.path.join(core_src, "requirements.txt"), base]
            if pinned := recipe[role].get("torch"):          # absent: ComfyUI's own torch lines, from the index
                req_files.append(os.path.join(work, f"torch-requirements-{role}.txt"))
                with open(req_files[-1], "w") as f:
                    f.write("\n".join(pinned) + "\n")
            lines = one_of([x for n in nodes_for(recipe, role) for x in reqs[n["name"]]], acc)
            node_file = os.path.join(work, f"node-requirements-{role}.txt")
            cons = recipe.get("constraints", []) + [x for n in nodes_for(recipe, role) for x in n.get("constraints", [])]
            extra = [os.path.join(work, f"overrides-{role}.txt")]
            with open(extra[0], "w") as f:
                f.write(overrides_for(acc))
            if cons:
                extra.append(os.path.join(work, f"constraints-{role}.txt"))
                with open(extra[-1], "w") as f:
                    f.write("\n".join(cons) + "\n")
            if lines and uv:
                with open(node_file, "w") as f:
                    f.write("\n".join(lines) + "\n")
                pip_probe = [f"--index-url={recipe[role]['index']}", f"--extra-index-url={PYPI}", "--index-strategy=unsafe-best-match"] + target
                for k in needed_through_dependencies(uv, req_files + [node_file], extra[1:], pip_probe, py, acc):
                    if not any(re.match(re.escape(k) + r"(?![A-Za-z0-9_.-])", x, re.I) for x in lines):
                        lines.append(k)
                        log(f"{role} venv: {k} added — a dependency of a pack asks for a variant of it")
            if lines:
                req_files.append(node_file)
                with open(node_file, "w") as f:
                    f.write("\n".join(lines) + "\n")
            stage = stage_nodes(work, role, ext, fetched, recipe)
            prefix = "farm-" + role
            nodes = same_tree(art, "nodes", stage, prefix)
            if nodes:
                log(f"nodes/{nodes}: same contents already published, reused")
            else:
                nodes = next_name(art, "nodes", prefix)
                publish_tree(art, "nodes", nodes, stage, recipe=recipe["recipe"],
                             sources={n["name"]: ({"archive": n["archive_sha256"], "file": n.get("archive_name") or
                                                   os.path.basename(n["archive"])} if "archive" in n else
                                                  {"repo": n["repo"], "commit": commits[n["name"]],
                                                   **({"submodules": subs[n["name"]]} if subs[n["name"]] else {})})
                                      for n in nodes_for(recipe, role)})
                log(f"nodes/{nodes}: published")
            v = recipe[role]
            inputs = venv_inputs(recipe, role, req_files, extra)
            venv = None if rebuild else next((n for n, m in sorted(ready(art, "venv").items())
                                              if m.get("inputs_sha256") == inputs), None)
            if venv:
                log(f"venv/{venv}: built from the same inputs, reused")
            else:
                venv = next_name(art, "venv", v["venv"])
                log(f"venv/{venv}: building ({v['accelerator']}, {v['index']}) — this takes a while")
                pip = [f"--index-url={v['index']}", f"--extra-index-url={PYPI}"] + (["--index-strategy=unsafe-best-match"] if uv else []) + target
                publish_venv(art, venv, py if uv else f"python{py}", req_files, v["accelerator"], v.get("arch", []),
                             pip + v.get("pip", []), uv, {"inputs_sha256": inputs, "recipe": recipe["recipe"]},
                             constraints=extra[1:], overrides=extra[:1])
                log(f"venv/{venv}: published")
            template[role] = {"core": core, "venv": venv, "nodes": [nodes], "flags": v.get("flags", []), "env": v.get("env", {})}
    pdir = os.path.join(art, "profiles")
    os.makedirs(pdir, exist_ok=True)
    for f in sorted(glob.glob(os.path.join(pdir, recipe["recipe"] + "-*.json"))):
        if not re.fullmatch(re.escape(recipe["recipe"]) + r"-\d+\.json", os.path.basename(f)):
            continue                                       # another recipe whose name starts with this one
        with open(f) as fh:
            old = json.load(fh)
        if all(old.get(r) == template.get(r) for r in ROLES):
            log(f"profiles/{os.path.basename(f)}: the same template exists")
            return old
    n = next_name(art, "profiles", recipe["recipe"]).rsplit("-", 1)[1]
    template["template"] = f"{recipe['recipe']}-{n}"
    template["created"] = dt.datetime.now(dt.timezone.utc).isoformat()
    path = os.path.join(pdir, template["template"] + ".json")
    with open(path, "x") as f:
        json.dump(template, f, indent=1)
    os.chmod(path, 0o444)
    log(f"profiles/{template['template']}.json: written — Dashboard → System → Rollout → Profiles → From a recipe")
    return template


def main(argv=None) -> int:
    # a venv in the store is its own copy, never hard links into uv's cache: the cache sits elsewhere (another mount in
    # the compose deployment, where uv warned "Failed to hardlink files" on every build) and may be cleaned
    os.environ.setdefault("UV_LINK_MODE", "copy")
    ap = argparse.ArgumentParser(prog="comfyfarm-publish")
    ap.add_argument("kind", choices=["core", "nodes", "frontend", "venv", "recipe"])
    ap.add_argument("art")
    ap.add_argument("name")
    ap.add_argument("--from", dest="src")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("-r", "--requirements", action="append", default=[])
    ap.add_argument("--accelerator", choices=["cuda", "rocm", "cpu"])
    ap.add_argument("--arch", action="append", default=[])
    ap.add_argument("--pip-arg", action="append", default=[])
    ap.add_argument("--uv", help="build with this uv binary instead of venv + pip")
    ap.add_argument("--rebuild", action="store_true", help="recipe: build new venvs even when the same inputs were built")
    ap.add_argument("--recipes", help="recipe: the directory holding recipes/ (default: next to this file, or the repository)")
    ap.add_argument("--roles", default="gpu,ui", help="recipe: build these halves (gpu, ui or both)")
    ap.add_argument("--nodes-json", help="recipe: a JSON list of node packs to add (the control-plane builder's)")
    ap.add_argument("--platform", help="recipe: wheels for this uv platform tag, e.g. x86_64-manylinux_2_35")
    ap.add_argument("--ext", help="recipe: the directory holding worker_ext/ and ui_ext/ (default: like --recipes)")
    a = ap.parse_args(argv)
    try:
        if a.kind == "recipe":
            extra = None
            if a.nodes_json:
                with open(a.nodes_json) as f:
                    extra = json.load(f)
                if not isinstance(extra, list):
                    raise Refused("--nodes-json: a list of node packs")
            m = publish_recipe(a.art, a.name, a.uv, a.rebuild, a.recipes, ext=a.ext, log=lambda x: print(x, file=sys.stderr),
                               roles=tuple(x.strip() for x in a.roles.split(",") if x.strip()), extra_nodes=extra,
                               platform=a.platform)
        elif a.kind == "venv":
            m = publish_venv(a.art, a.name, a.python, a.requirements, a.accelerator, a.arch, a.pip_arg, a.uv)
        else:
            if not a.src:
                ap.error("--from is required")
            m = publish_tree(a.art, a.kind, a.name, a.src)
    except Refused as e:
        print(f"publish: refused: {e}", file=sys.stderr)
        return 2
    print(json.dumps(m, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
