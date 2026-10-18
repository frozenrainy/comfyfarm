"""Publish artifacts into the store (contract §2) — a minimal stand-in for the builder (§8), without its gates:

    python3 publish.py core     <ART> <name> --from <unpacked ComfyUI release>
    python3 publish.py nodes    <ART> <name> --from <dir of custom node packages>
    python3 publish.py frontend <ART> <name> --from <built frontend dist>
    python3 publish.py venv     <ART> <name> --python python3.12 -r requirements.txt [--accelerator rocm --arch gfx1201]
                                [--pip-arg=--index-url=…] [--uv /path/to/uv]   (uv: same rules, its wheel cache)

Rules kept: a name is never reused (an existing directory is refused); READY is written last, only after the copy or
build and the checks, and the artifact is made read-only first; a venv is built at its final path (absolute paths are
baked in) and must not reference anything outside itself (.pth files pointing elsewhere, editable installs); a core
has no symlinks (the release archive as is) and a node or frontend symlink stays inside its artifact. Standard
library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import shutil
import socket
import subprocess
import sys

try:
    from .launcher import CONTRACT, NAME, tree_sha256
except ImportError:                                        # run as a copied single file next to launcher.py
    from launcher import CONTRACT, NAME, tree_sha256     # type: ignore


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


def publish_tree(art: str, kind: str, name: str, src: str) -> dict:
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
    m = write_manifest(path, kind, name)
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
                 arch: list[str], pip_args: list[str], uv: str | None = None) -> dict:
    """Built at its final path, with its interpreter inside it (D-42), so every host that mounts the store at the same
    path can run it — not only the build host."""
    path = target(art, "venv", name)
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
    m = write_manifest(path, "venv", name, **extra)
    seal(path)
    return m


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="comfyfarm-publish")
    ap.add_argument("kind", choices=["core", "nodes", "frontend", "venv"])
    ap.add_argument("art")
    ap.add_argument("name")
    ap.add_argument("--from", dest="src")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("-r", "--requirements", action="append", default=[])
    ap.add_argument("--accelerator", choices=["cuda", "rocm", "cpu"])
    ap.add_argument("--arch", action="append", default=[])
    ap.add_argument("--pip-arg", action="append", default=[])
    ap.add_argument("--uv", help="build with this uv binary instead of venv + pip")
    a = ap.parse_args(argv)
    try:
        if a.kind == "venv":
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
