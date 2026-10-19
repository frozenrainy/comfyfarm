"""Builds from the dashboard on a worker host (opt-in at setup, docs/design/14): standard library only, installed by
worker setup into /opt/comfyfarm/worker/ (root-owned) with publish.py, the recipes and the two extensions.

The router may only *name* a recipe that root installed here — never send one — and the service account that runs
ComfyUI never writes the artifact store:

  agent `build <recipe>` (service account)  →  a file named <recipe> in REQUESTS (it may create files there, not list)
  comfyfarm-build.path (root)               →  comfyfarm-build.service runs `build.py run <ART> <uv>` as root, which
                                               takes each request, checks the name against the installed recipes and
                                               runs `publish.py recipe` as BUILD_USER (the store's owner)
  agent `build-status <recipe>`             →  STATUS/<recipe>.json and the end of its log
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pwd
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD_USER = "comfyfarm-build"
BUILD_HOME = "/var/lib/comfyfarm-build"
REQUESTS = f"{BUILD_HOME}/requests"
STATUS = f"{BUILD_HOME}/status"
RECIPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
LOG_TAIL = 4000


def installed(recipe: str, here: str | None = None) -> bool:
    here = here or HERE
    return bool(RECIPE.match(recipe)) and os.path.isfile(os.path.join(here, "recipes", recipe + ".json"))


def recipes(here: str | None = None) -> list[str]:
    here = here or HERE
    d = os.path.join(here, "recipes")
    return sorted(n[:-5] for n in os.listdir(d) if n.endswith(".json") and installed(n[:-5], here)) if os.path.isdir(d) else []


def request(recipe: str, requests: str | None = None, here: str | None = None) -> str:
    """The agent's half (service account): ask for a build of an installed recipe."""
    requests, here = requests or REQUESTS, here or HERE
    if not installed(recipe, here):
        raise ValueError(f"no recipe {recipe!r} on this host (installed: {', '.join(recipes(here)) or 'none'})")
    with open(os.path.join(requests, recipe), "w") as f:
        f.write(dt.datetime.now(dt.timezone.utc).isoformat() + "\n")
    return f"build of {recipe} requested"


def status(recipe: str, status_dir: str | None = None) -> dict:
    status_dir = status_dir or STATUS
    if not RECIPE.match(recipe):
        raise ValueError(f"bad recipe name {recipe!r}")
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


def _write_status(status_dir: str, recipe: str, st: dict, owner: tuple[int, int] | None):
    path = os.path.join(status_dir, recipe + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.chmod(tmp, 0o644)
    if owner:
        os.chown(tmp, *owner)
    os.replace(tmp, path)


def run(art: str, uv: str, requests: str | None = None, status_dir: str | None = None, here: str | None = None,
        as_user: str | None = BUILD_USER, runner=subprocess.run) -> list[str]:
    """The root half (comfyfarm-build.service): every pending request, one after the other."""
    requests, status_dir, here = requests or REQUESTS, status_dir or STATUS, here or HERE
    done = []
    owner = None
    if as_user:
        pw = pwd.getpwnam(as_user)
        owner = (pw.pw_uid, pw.pw_gid)
    for name in sorted(os.listdir(requests)):
        try:
            os.unlink(os.path.join(requests, name))
        except OSError:
            continue
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        if not installed(name, here):                      # anything else in the drop box is refused, never run
            if RECIPE.match(name):
                _write_status(status_dir, name, {"recipe": name, "state": "failed", "finished": now,
                                                 "error": "no such recipe on this host"}, owner)
            continue
        _write_status(status_dir, name, {"recipe": name, "state": "running", "started": now}, owner)
        log_path = os.path.join(status_dir, name + ".log")
        argv = [sys.executable, os.path.join(here, "publish.py"), "recipe", art, name, "--uv", uv, "--recipes", here]
        if as_user:
            argv = ["runuser", "-u", as_user, "--", "env", f"HOME={BUILD_HOME}", f"UV_CACHE_DIR={BUILD_HOME}/uv-cache",
                    "PATH=/usr/local/bin:/usr/bin:/bin"] + argv
        with open(log_path, "wb") as log:
            if owner:
                os.chown(log_path, *owner)
            p = runner(argv, stdout=subprocess.PIPE, stderr=log, cwd="/")
        template = None
        try:
            template = json.loads(p.stdout or b"null")
        except ValueError:
            pass
        _write_status(status_dir, name, {"recipe": name, "state": "done" if p.returncode == 0 else "failed",
                                         "started": now, "finished": dt.datetime.now(dt.timezone.utc).isoformat(),
                                         "rc": p.returncode, "template": (template or {}).get("template")}, owner)
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
