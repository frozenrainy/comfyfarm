"""Builds from the dashboard on a worker host (opt-in at setup, docs/design/14): standard library only, installed by
worker setup into /opt/comfyfarm/worker/ (root-owned) with publish.py, the recipes and the two extensions.

The router may only *name* a recipe that root installed here — never send one — and the service account that runs
ComfyUI never writes the artifact store:

  agent `build <recipe>` (service account)  →  a file named <recipe> in REQUESTS (it may create files there, not list)
  comfyfarm-build.path (root)               →  comfyfarm-build.service runs `build.py run <ART> <uv>` as root, which
                                               takes each request, checks the name against the installed recipes and
                                               runs `publish.py recipe` as BUILD_USER (the store's owner)
  agent `build-status <recipe>`             →  queued while the request waits, else STATUS/<recipe>.json + log tail

Everything root writes (STATUS, the logs) lives in directories root owns and the build account cannot write: a file
there is never a symlink the build account planted, and nothing root creates is handed to the build account. The
build account's own home is the HOME subdirectory (uv's interpreter store), nothing else.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pwd
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_USER = "comfyfarm-build"
BUILD_HOME = "/var/lib/comfyfarm-build"       # root, 0711
HOME = f"{BUILD_HOME}/home"                   # the build account's own (uv, pip caches)
REQUESTS = f"{BUILD_HOME}/requests"           # root:<service account>, 0730
STATUS = f"{BUILD_HOME}/status"               # root, 0755
RECIPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
LOG_TAIL = 4000
NOFOLLOW = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)


def installed(recipe: str, here: str | None = None) -> bool:
    here = here or HERE
    return (bool(RECIPE.match(recipe)) and not recipe.endswith(".json")
            and os.path.isfile(os.path.join(here, "recipes", recipe + ".json")))


def recipes(here: str | None = None) -> list[str]:
    here = here or HERE
    d = os.path.join(here, "recipes")
    return sorted(n[:-5] for n in os.listdir(d) if n.endswith(".json") and installed(n[:-5], here)) if os.path.isdir(d) else []


def request(recipe: str, requests: str | None = None, here: str | None = None) -> str:
    """The agent's half (service account): ask for a build of an installed recipe."""
    requests, here = requests or REQUESTS, here or HERE
    if not installed(recipe, here):
        raise ValueError(f"no recipe {recipe!r} on this host (installed: {', '.join(recipes(here)) or 'none'})")
    try:
        fd = os.open(os.path.join(requests, recipe), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return f"build of {recipe} is already queued"
    with os.fdopen(fd, "w") as f:
        f.write(dt.datetime.now(dt.timezone.utc).isoformat() + "\n")
    return f"build of {recipe} requested"


def status(recipe: str, status_dir: str | None = None, requests: str | None = None) -> dict:
    status_dir, requests = status_dir or STATUS, requests or REQUESTS
    if not RECIPE.match(recipe):
        raise ValueError(f"bad recipe name {recipe!r}")
    if os.path.exists(os.path.join(requests, recipe)):       # a request waits: the last run's state is not this one
        return {"recipe": recipe, "state": "queued"}
    try:
        with open(os.path.join(status_dir, recipe + ".json")) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {"recipe": recipe, "state": "none"}
    try:
        with open(os.path.join(status_dir, recipe + ".log"), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - LOG_TAIL))
            st["log"] = f.read().decode(errors="replace")
    except OSError:
        pass
    return st


def _write_status(status_dir: str, recipe: str, st: dict):
    path = os.path.join(status_dir, recipe + ".json")
    tmp = path + ".tmp"
    with os.fdopen(os.open(tmp, NOFOLLOW, 0o644), "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, path)


def _take(requests: str, name: str) -> bool:
    """Remove one entry of the drop box; True when it was a plain file (a request). Anything else the service
    account could put there — a directory, a link — is removed without being looked at."""
    path = os.path.join(requests, name)
    try:
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.isfile(path):
            os.unlink(path)
            return True
        else:
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass
    return False


def run(art: str, uv: str, requests: str | None = None, status_dir: str | None = None, here: str | None = None,
        as_user: str | None = BUILD_USER, runner=subprocess.run, home: str | None = None) -> list[str]:
    """The root half (comfyfarm-build.service): every pending request, one after the other."""
    requests, status_dir, here, home = requests or REQUESTS, status_dir or STATUS, here or HERE, home or HOME
    done = []
    for name in sorted(os.listdir(requests)):
        if not _take(requests, name) or not installed(name, here):
            continue                                        # not a request for an installed recipe: never run
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        _write_status(status_dir, name, {"recipe": name, "state": "running", "started": now})
        argv = [sys.executable, os.path.join(here, "publish.py"), "recipe", art, name, "--uv", uv, "--recipes", here]
        if as_user:
            pwd.getpwnam(as_user)                            # KeyError here is a setup problem, not a request
            argv = ["runuser", "-u", as_user, "--", "env", f"HOME={home}", f"UV_CACHE_DIR={home}/uv-cache",
                    "PATH=/usr/local/bin:/usr/bin:/bin"] + argv
        with os.fdopen(os.open(os.path.join(status_dir, name + ".log"), NOFOLLOW, 0o644), "wb") as log:
            p = runner(argv, stdout=subprocess.PIPE, stderr=log, cwd="/")
        template = None
        try:
            template = json.loads(p.stdout or b"null")
        except ValueError:
            pass
        _write_status(status_dir, name, {"recipe": name, "state": "done" if p.returncode == 0 else "failed",
                                         "started": now, "finished": dt.datetime.now(dt.timezone.utc).isoformat(),
                                         "rc": p.returncode, "template": (template or {}).get("template")})
        done.append(name)
    return done


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 3 and args[0] == "run":
        run(args[1], args[2])
        return 0
    print("usage: build.py run <ART> <uv>   (comfyfarm-build.service; requests come through the agent)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
