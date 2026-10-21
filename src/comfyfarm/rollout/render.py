"""Render (contract §3) and badge (§5): what an instance should run, and whether it runs it.

The render is built from a profile (artifact names, flags, env) and an instance (paths, address, platform). Before an
assignment the router validates the render with the launcher's own rules (minus the READY files it cannot see), so an
assignment the launcher would refuse is refused here first.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import posixpath

from .. import launcher

CONTRACT = launcher.CONTRACT
REV_SLOT = "{rev}"
PATH_KEYS = ("art", "local", "models", "input", "output", "user", "temp")
OPTIONAL_PATH_KEYS = ("model_store",)                    # the farm's models (docs/design/17); setups from dev.17 on
WRITABLE_KEYS = ("input", "output", "user", "temp")     # users write here (launcher.WRITABLE)
MANAGED = {"--listen", "--port", "--multi-user", "--models-directory", "--extra-model-paths-config", "--input-directory",
           "--output-directory", "--user-directory", "--temp-directory", "--front-end-root", "--base-directory"}


class BadProfile(ValueError):
    pass


def _join(win: bool, *parts: str) -> str:
    sep = "\\" if win else "/"
    return sep.join([parts[0].rstrip("/\\")] + [p.strip("/\\") for p in parts[1:]])


def build(profile: dict, inst: dict) -> dict:
    win = inst["platform"] == "windows"
    j = lambda *p: _join(win, *p)                          # noqa: E731
    paths, name = inst["paths"], inst["instance"]
    art, local = paths["art"], paths["local"]
    core_dst = j(local, "core", profile["core"])
    run_dir = j(local, "run", name)
    node_dirs = [j(local, "nodes", n) for n in profile["nodes"]]
    yaml_path = j(run_dir, f"extra_model_paths.{REV_SLOT}.yaml")
    python = j(art, "venv", profile["venv"], "Scripts", "python.exe") if win else j(art, "venv", profile["venv"], "bin", "python")
    argv = [python, j(core_dst, "main.py"), "--listen", inst["listen"], "--port", str(inst["port"]), "--multi-user",
            "--models-directory", paths["models"], "--extra-model-paths-config", yaml_path,
            "--input-directory", paths["input"], "--output-directory", paths["output"],
            "--user-directory", paths["user"], "--temp-directory", paths["temp"]]
    if profile.get("frontend"):
        argv += ["--front-end-root", j(art, "frontend", profile["frontend"])]
    argv += list(profile.get("flags") or [])
    sync = [{"kind": "core", "name": profile["core"], "src": j(art, "core", profile["core"]), "dst": core_dst}]
    sync += [{"kind": "nodes", "name": n, "src": j(art, "nodes", n), "dst": d} for n, d in zip(profile["nodes"], node_dirs)]
    require = [j(art, "core", profile["core"], "READY"), j(art, "venv", profile["venv"], "READY")]
    require += [j(art, "nodes", n, "READY") for n in profile["nodes"]]
    if profile.get("frontend"):
        require.append(j(art, "frontend", profile["frontend"], "READY"))
    render = {
        "contract": CONTRACT, "instance": name, "profile": profile["name"], "role": inst["role"],
        "argv": argv, "cwd": core_dst, "env": dict(profile.get("env") or {}), "sync": sync,
        "files": {yaml_path: launcher.nodes_yaml(node_dirs, paths.get("model_store"))}, "require": require,
        "mkdirs": [run_dir, paths["input"], paths["output"], paths["user"], paths["temp"]],
    }
    if win:
        render["platform"] = "windows"                     # §9.2; absent = posix, so v1 renders stay byte-identical
    # §3: rev over the render without rev, the yaml path still a placeholder; then the rev goes into the path
    rev = hashlib.sha1(json.dumps(render, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    real = yaml_path.replace(REV_SLOT, rev)
    render["argv"] = [real if a == yaml_path else a for a in argv]
    render["files"] = {real: render["files"][yaml_path]}
    render["rev"] = rev
    return render


def path_problems(paths: dict, win: bool) -> list[str]:
    """The launcher's rule for user-writable directories, for both platforms: never inside <ART> or the launcher-managed
    parts of <LOCAL> (core, nodes, frontend, run), and never a parent of them (an upload's subfolder would reach the
    code below)."""
    norm = (lambda p: ntpath.normcase(ntpath.normpath(p))) if win else posixpath.normpath     # noqa: E731
    sep = "\\" if win else "/"

    def inside(a: str, b: str) -> bool:
        a, b = norm(a), norm(b)
        return a == b or a.startswith(b.rstrip(sep) + sep)
    code = {"paths.art": paths["art"], **{f"paths.local/{d}": paths["local"].rstrip("/\\") + sep + d
                                          for d in launcher.MANAGED_LOCAL}}
    if paths.get("model_store"):                         # models are loaded from there: users must not write into it
        code["paths.model_store"] = paths["model_store"]
    return [f"paths.{k} overlaps {name}" for k in WRITABLE_KEYS for name, c in code.items()
            if inside(paths[k], c) or inside(c, paths[k])]


def model_store_problem(paths: dict, win: bool) -> str | None:
    """The farm's models (docs/design/17) are a directory of their own: the site's models stay untouched, and the
    artifact store is the copies' (or the share's) alone."""
    ms = paths.get("model_store")
    if not ms:
        return None
    norm = (lambda p: ntpath.normcase(ntpath.normpath(p))) if win else posixpath.normpath     # noqa: E731
    sep = "\\" if win else "/"

    def inside(a: str, b: str) -> bool:
        a, b = norm(a), norm(b)
        return a == b or a.startswith(b.rstrip(sep) + sep)
    for k in ("models", "art"):
        if paths.get(k) and (inside(ms, paths[k]) or inside(paths[k], ms)):
            return (f"paths.model_store overlaps paths.{k} — the farm's models need a directory of their own, outside "
                    "the site's models and the artifact store")
    return None


def check(profile: dict, inst: dict) -> dict:
    """Build and validate; raises BadProfile with the launcher's reason."""
    flags = list(profile.get("flags") or [])
    if bad := [f for f in flags if f in MANAGED]:
        raise BadProfile(f"flags set by the instance cannot be overridden by a profile: {bad}")
    render = build(profile, inst)
    if inst["platform"] == "posix":
        prefixes = [inst["paths"][k] for k in ("models", "input", "output", "user", "temp")]
        try:
            launcher.validate(render, instance=inst["instance"], art=inst["paths"]["art"], local=inst["paths"]["local"],
                              prefixes=prefixes, check_files=False, model_store=inst["paths"].get("model_store"))
        except launcher.Refused as e:
            raise BadProfile(str(e))
    return render


def badge(render: dict | None, worker_argv) -> str:
    """§5: `system.argv` (no interpreter) against the render's argv[1:]. The rev in the yaml file name makes a venv-only
    change visible."""
    if render is None:
        return "unassigned"
    if not isinstance(worker_argv, list):
        return "unknown"
    return "match" if worker_argv == render["argv"][1:] else "differs"
