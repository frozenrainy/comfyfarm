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


def publish_venv(art: str, name: str, python: str, requirements: list[str], accelerator: str | None,
                 arch: list[str], pip_args: list[str], uv: str | None = None, manifest_extra: dict | None = None) -> dict:
    """Built at its final path, with its interpreter inside it (D-42), so every host that mounts the store at the same
    path can run it — not only the build host."""
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
        for r in requirements:
            subprocess.run(pip + ["install"] + target_py + ["-r", r] + pip_args, check=True)
        subprocess.run(pip + ["check"] + target_py, check=True)
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


def find_dir(name: str, given: str | None) -> str:
    """recipes/, worker_ext/, ui_ext/: given, next to this file (copied onto a host), or the repository root."""
    for d in ([given] if given else []) + [HERE, os.path.join(HERE, "..", "..")]:
        if os.path.isdir(os.path.join(d, name)):
            return os.path.abspath(d)
    raise Refused(f"no {name}/ next to publish.py or in the repository — run it from a ComfyFarm checkout")


REPO = re.compile(r"^(https://[A-Za-z0-9][A-Za-z0-9._~:/@-]*|/[A-Za-z0-9._][A-Za-z0-9._/-]*)$")   # https, or a local path
RELEASE = re.compile(r"^(v?[0-9][A-Za-z0-9._-]*|[0-9a-f]{40})$")                                     # a tag or a commit
COMMIT = re.compile(r"^[0-9a-f]{40}$")
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
        if bad := [f for f in v.get("flags", []) if not isinstance(f, str) or (f.startswith("--") and f not in OPTIONS)]:
            raise Refused(f"{who}: {role} flags the launcher refuses: {bad}")
        if bad := [why for k, x in (v.get("env") or {}).items() if (why := env_problem(str(k), x))]:
            raise Refused(f"{who}: {role} environment the launcher refuses: {'; '.join(bad)}")
    for n in r.get("nodes", []):
        if (not isinstance(n, dict) or not NAME.match(str(n.get("name", ""))) or not REPO.match(str(n.get("repo", "")))
                or not COMMIT.match(str(n.get("commit", "")))):
            raise Refused(f"{who}: a node needs name, repo (https://… or a local path) and a full commit (no branches: "
                          "a recipe is pinned)")
        if not all(isinstance(x, str) and "\n" not in x and not x.startswith("-") for x in n.get("requirements", [])):
            raise Refused(f"{who}: node {n['name']} requirements must be requirement lines, not options")
    return r


def fetch(repo: str, ref: str, dest: str) -> tuple[str, str]:
    """The tree of `ref` in `repo`, as `git archive` gives it (no .git) — the same bytes every time — and the commit
    it resolved to. repo and ref come after --end-of-options: git never reads them as options."""
    src = dest + ".git-src"
    subprocess.run(["git", "init", "-q", src], check=True)
    subprocess.run(["git", "-C", src, "fetch", "-q", "--depth", "1", "--end-of-options", repo, ref], check=True)
    commit = subprocess.run(["git", "-C", src, "rev-parse", "FETCH_HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    os.makedirs(dest)
    archive = subprocess.run(["git", "-C", src, "archive", "FETCH_HEAD"], check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", dest], input=archive, check=True)
    shutil.rmtree(src)
    return dest, commit


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
    for n in recipe.get("nodes", []):
        if role == "gpu" or n.get("ui"):
            shutil.copytree(fetched[n["name"]], os.path.join(stage, n["name"]), symlinks=True, ignore=ignore)
    return stage


def venv_inputs(recipe: dict, role: str, requirements: list[str]) -> str:
    """What decides a venv's contents. A venv build is not byte-for-byte repeatable, so reuse goes by its inputs."""
    v = recipe[role]
    blob = {"python": recipe.get("python", "3.12"), "accelerator": v["accelerator"], "arch": v.get("arch", []),
            "index": v["index"], "pip": v.get("pip", []), "requirements": [open(r).read() for r in requirements]}
    return hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()


def publish_recipe(art: str, name: str, uv: str | None = None, rebuild: bool = False, recipes: str | None = None,
                   ext: str | None = None, log=print) -> dict:
    """Publish what the recipe names, reusing what is already there with the same content, and write a profile
    template (<ART>/profiles/<template>.json, ComfyFarm's own — not part of the contract) for the dashboard."""
    recipe, ext = load_recipe(name, recipes), find_dir("worker_ext", ext)
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
        fetched, commits = {}, {}
        for n in recipe.get("nodes", []):
            fetched[n["name"]], commits[n["name"]] = fetch(n["repo"], n["commit"], os.path.join(work, "node-" + n["name"]))
        node_reqs = []
        for n in recipe.get("nodes", []):
            node_reqs += n.get("requirements", [])
        req_files = [os.path.join(core_src, "requirements.txt")]
        if node_reqs:
            req_files.append(os.path.join(work, "node-requirements.txt"))
            with open(req_files[-1], "w") as f:
                f.write("\n".join(node_reqs) + "\n")
        template = {"recipe": recipe["recipe"], "about": recipe.get("about", ""), "comfyui": c["ref"]}
        for role in ROLES:
            stage = stage_nodes(work, role, ext, fetched, recipe)
            prefix = "farm-" + role
            nodes = same_tree(art, "nodes", stage, prefix)
            if nodes:
                log(f"nodes/{nodes}: same contents already published, reused")
            else:
                nodes = next_name(art, "nodes", prefix)
                publish_tree(art, "nodes", nodes, stage, recipe=recipe["recipe"],
                             sources={n["name"]: {"repo": n["repo"], "commit": commits[n["name"]]}
                                      for n in recipe.get("nodes", []) if role == "gpu" or n.get("ui")})
                log(f"nodes/{nodes}: published")
            v = recipe[role]
            inputs = venv_inputs(recipe, role, req_files)
            venv = None if rebuild else next((n for n, m in sorted(ready(art, "venv").items())
                                              if m.get("inputs_sha256") == inputs), None)
            if venv:
                log(f"venv/{venv}: built from the same inputs, reused")
            else:
                venv = next_name(art, "venv", v["venv"])
                log(f"venv/{venv}: building ({v['accelerator']}, {v['index']}) — this takes a while")
                pip = [f"--index-url={v['index']}", f"--extra-index-url={PYPI}"] + (["--index-strategy=unsafe-best-match"] if uv else [])
                publish_venv(art, venv, py if uv else f"python{py}", req_files, v["accelerator"], v.get("arch", []),
                             pip + v.get("pip", []), uv, {"inputs_sha256": inputs, "recipe": recipe["recipe"]})
                log(f"venv/{venv}: published")
            template[role] = {"core": core, "venv": venv, "nodes": [nodes], "flags": v.get("flags", []), "env": v.get("env", {})}
    pdir = os.path.join(art, "profiles")
    os.makedirs(pdir, exist_ok=True)
    for f in sorted(glob.glob(os.path.join(pdir, recipe["recipe"] + "-*.json"))):
        if not re.fullmatch(re.escape(recipe["recipe"]) + r"-\d+\.json", os.path.basename(f)):
            continue                                       # another recipe whose name starts with this one
        with open(f) as fh:
            old = json.load(fh)
        if all(old.get(r) == template[r] for r in ROLES):
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
    a = ap.parse_args(argv)
    try:
        if a.kind == "recipe":
            m = publish_recipe(a.art, a.name, a.uv, a.rebuild, a.recipes, log=lambda x: print(x, file=sys.stderr))
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
