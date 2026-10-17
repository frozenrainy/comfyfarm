"""Publish artifacts into the store (contract §2) — a minimal stand-in for the builder (§8), without its gates:

    python3 publish.py core     <ART> <name> --from <unpacked ComfyUI release>
    python3 publish.py nodes    <ART> <name> --from <dir of custom node packages>
    python3 publish.py frontend <ART> <name> --from <built frontend dist>
    python3 publish.py venv     <ART> <name> --python python3.12 -r requirements.txt [--accelerator rocm --arch gfx1201]
                                [--pip-arg=--index-url=…] [--uv /path/to/uv]   (uv: same rules, its wheel cache)

Rules kept: a name is never reused (an existing directory is refused); READY is written last, only after the copy or
build and the checks; a venv is built at its final path (absolute paths are baked in) and must not reference anything
outside itself (.pth files pointing elsewhere, editable installs). Standard library only.
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
    os.rename(tmp, path)
    m = write_manifest(path, kind, name)
    open(os.path.join(path, "READY"), "w").close()
    return m


def venv_escapes(path: str) -> list[str]:
    """.pth lines and editable installs that point outside the venv."""
    bad = []
    for d, _, names in os.walk(path):
        for n in names:
            if n.endswith(".pth"):
                with open(os.path.join(d, n), errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith(("#", "import")) and os.path.isabs(line) and \
                                not os.path.abspath(line).startswith(path + os.sep):
                            bad.append(f"{n}: {line}")
            if n == "direct_url.json":
                with open(os.path.join(d, n)) as f:
                    if json.load(f).get("dir_info", {}).get("editable"):
                        bad.append(f"editable install: {os.path.basename(d)}")
    return bad


def publish_venv(art: str, name: str, python: str, requirements: list[str], accelerator: str | None,
                 arch: list[str], pip_args: list[str], uv: str | None = None) -> dict:
    """The interpreter itself stays outside (a venv links to its base Python): every worker that uses the store
    needs that base at the same path."""
    path = target(art, "venv", name)
    py = os.path.join(path, "bin", "python")
    pip = [uv, "pip"] if uv else [py, "-m", "pip"]
    target_py = ["--python", py] if uv else []
    if uv:
        subprocess.run([uv, "venv", "--python", python, path], check=True)   # at its final path
    else:
        subprocess.run([python, "-m", "venv", path], check=True)             # at its final path
    try:
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
    open(os.path.join(path, "READY"), "w").close()
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
