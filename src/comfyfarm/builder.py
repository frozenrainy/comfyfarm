"""Builds on the control plane (docs/design/15): the `builder` service of the compose deployment. Its own account owns
the artifact store; the router only *asks*, by writing a request file, and reads what this process writes back:

  REQUESTS/<id>.json   written by the router: {"kind": "build", "recipe", "roles", "nodes"} or {"kind": "resolve",
                       "repo", "ref"} — the router may write here, nothing else (its account; this one only reads)
  STATUS/<id>.json     written here: state queued → running → done | failed, the template a build made, a resolved commit
  STATUS/<id>.log      the build's log
  STATUS/policy.json   what this builder takes: its recipes, the node pack sources, and a heartbeat

What the router cannot change: the recipes are the image's own, and node packs picked in the dashboard are built only
from repositories under COMFYFARM_BUILD_NODE_SOURCES (comma-separated URL prefixes; empty = no dashboard node packs).
That setting is this service's environment — the operator's, in the compose file — so a router that is taken over
cannot widen it (D-37 holds within the sources the operator allowed).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time

from . import publish

REQUEST = re.compile(r"^[0-9]{10,20}-[0-9a-f]{8}$")
REF = re.compile(r"^(?!-)[A-Za-z0-9][A-Za-z0-9._/+-]{0,120}$")
HTTPS = re.compile(r"^https://[A-Za-z0-9][A-Za-z0-9._~:/@-]*$")
NODE_KEYS = {"name", "repo", "commit", "ui", "requirements_file", "constraints"}
MAX_REQUEST = 256 * 1024
MAX_NODES = 200
HEARTBEAT = 30.0
LOG_TAIL = 6000


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json(path: str, doc: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


class Refused(Exception):
    pass


def say(line: str) -> None:
    """One line per request start and end: `docker compose logs builder` tells what it did and when."""
    print(f"comfyfarm builder: {line}", flush=True)


class Builder:
    def __init__(self, requests: str, status: str, art: str, share: str, uv: str | None, node_sources: list[str],
                 platform_tag: str | None = None, runner=subprocess.run):
        self.requests, self.status, self.art, self.share, self.uv = requests, status, art, share, uv
        self.node_sources = [s for s in node_sources if s]
        self.platform = platform_tag
        self.run = runner
        self.lock = threading.Lock()

    # ---- what the router may ask -------------------------------------------------------------------------------
    def recipes(self) -> list[str]:
        d = os.path.join(self.share, "recipes")
        return sorted(n[:-5] for n in os.listdir(d) if n.endswith(".json")) if os.path.isdir(d) else []

    def allowed_source(self, repo) -> bool:
        return (isinstance(repo, str) and bool(HTTPS.match(repo)) and ".." not in repo
                and any(repo.startswith(p) for p in self.node_sources))

    def check_nodes(self, nodes) -> list[dict]:
        if not isinstance(nodes, list) or len(nodes) > MAX_NODES:
            raise Refused(f"nodes: a list of at most {MAX_NODES} node packs")
        if nodes and not self.node_sources:
            raise Refused("node packs from the dashboard are off for this farm (COMFYFARM_BUILD_NODE_SOURCES is empty)")
        out = []
        for n in nodes:
            if not isinstance(n, dict) or set(n) - NODE_KEYS:
                raise Refused(f"node pack: only {', '.join(sorted(NODE_KEYS))}")
            if not self.allowed_source(n.get("repo")):
                raise Refused(f"node pack {n.get('name')!r}: {n.get('repo')!r} is not under the allowed sources "
                              f"({', '.join(self.node_sources)})")
            out.append(n)                     # names, commits, files and constraints: publish.check_recipe
        return out

    def about(self) -> dict:
        out = {}
        for r in self.recipes():
            try:
                with open(os.path.join(self.share, "recipes", r + ".json")) as f:
                    out[r] = str(json.load(f).get("about", ""))[:200]
            except (OSError, ValueError):
                out[r] = ""
        return out

    def policy(self) -> dict:
        return {"recipes": self.recipes(), "about": self.about(), "node_sources": self.node_sources,
                "platform": self.platform, "alive": now(), "pid": os.getpid()}

    # ---- the work ----------------------------------------------------------------------------------------------
    def pending(self, kind: str) -> list[str]:
        out = []
        for n in sorted(os.listdir(self.requests)):
            rid = n[:-5]
            if not n.endswith(".json") or not REQUEST.match(rid) or os.path.exists(os.path.join(self.status, rid + ".json")):
                continue
            try:
                with open(os.path.join(self.requests, n)) as f:
                    if json.loads(f.read(MAX_REQUEST)).get("kind") == kind:
                        out.append(rid)
            except (OSError, ValueError, AttributeError):
                write_json(os.path.join(self.status, rid + ".json"), {"id": rid, "state": "failed", "error": "unreadable request"})
        return out

    def take(self, rid: str) -> dict:
        path = os.path.join(self.requests, rid + ".json")
        if os.path.getsize(path) > MAX_REQUEST:
            raise Refused("request too large")
        with open(path) as f:
            req = json.load(f)
        if not isinstance(req, dict):
            raise Refused("a request is a JSON object")
        return req

    def handle(self, rid: str) -> dict:
        st = {"id": rid, "state": "running", "started": now()}
        t0 = time.monotonic()
        try:
            req = self.take(rid)
            st["kind"] = req.get("kind")
            write_json(os.path.join(self.status, rid + ".json"), st)
            say(f"{rid}: {req.get('kind')} " + (f"{req.get('recipe')} ({','.join(req.get('roles') or publish.ROLES)}, "
                                               f"{len(req.get('nodes') or [])} node pack(s))" if req.get("kind") == "build"
                                               else f"{req.get('repo')} {req.get('ref') or 'HEAD'}"))
            if req.get("kind") == "resolve":
                st.update(self.resolve(req))
            elif req.get("kind") == "build":
                st.update(self.build(rid, req))
            else:
                raise Refused(f"unknown request kind {req.get('kind')!r}")
            st["state"] = "done"
        except (Refused, publish.Refused) as e:
            st.update(state="failed", error=str(e))
        except Exception as e:                       # never leave a request running
            st.update(state="failed", error=f"{type(e).__name__}: {e}")
        st["finished"] = now()
        write_json(os.path.join(self.status, rid + ".json"), st)
        say(f"{rid}: {st['state']} after {time.monotonic() - t0:.0f} s"
            + (f" — template {st['template']}" if st.get("template") else f" — {st['error']}" if st.get("error") else ""))
        return st

    def resolve(self, req: dict) -> dict:
        """A branch, tag or HEAD of an allowed repository → the commit it names now (the dashboard pins that)."""
        repo, ref = req.get("repo"), (req.get("ref") or "HEAD").strip()
        if not self.node_sources:
            raise Refused("node packs from the dashboard are off for this farm (COMFYFARM_BUILD_NODE_SOURCES is empty)")
        if not self.allowed_source(repo):
            raise Refused(f"{repo!r} is not under the allowed sources ({', '.join(self.node_sources)})")
        if re.fullmatch(r"[0-9a-f]{40}", ref):
            return {"repo": repo, "commit": ref, "ref": ref}
        if not REF.match(ref):
            raise Refused(f"bad ref {ref!r}")
        env = {**os.environ, "GIT_ALLOW_PROTOCOL": "https", "GIT_TERMINAL_PROMPT": "0"}
        p = publish.git_net(["git", "ls-remote", "--end-of-options", repo, ref], env)
        refs = {}
        for line in p.stdout.splitlines():
            sha, _, name = line.partition("\t")
            refs[name.strip()] = sha.strip()
        for name in (f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}", f"refs/heads/{ref}", ref):
            if name in refs and re.fullmatch(r"[0-9a-f]{40}", refs[name]):
                return {"repo": repo, "commit": refs[name], "ref": ref}
        raise Refused(f"{ref!r} is not a branch or tag of {repo}")

    def build(self, rid: str, req: dict) -> dict:
        recipe = req.get("recipe")
        if recipe not in self.recipes():
            raise Refused(f"no recipe {recipe!r} here (the image's: {', '.join(self.recipes()) or 'none'})")
        roles = req.get("roles") or list(publish.ROLES)
        if not isinstance(roles, list) or not roles or set(roles) - set(publish.ROLES):
            raise Refused("roles: gpu, ui or both")
        nodes = self.check_nodes(req.get("nodes") or [])
        argv = [sys.executable, "-m", "comfyfarm.publish", "recipe", self.art, recipe, "--recipes", self.share,
                "--ext", self.share, "--roles", ",".join(roles)]
        if self.uv:
            argv += ["--uv", self.uv]
        if self.platform:
            argv += ["--platform", self.platform]
        if nodes:
            nf = os.path.join(self.status, rid + ".nodes.json")
            write_json(nf, nodes)                    # what was built, next to its log
            argv += ["--nodes-json", nf]
        with self.lock, open(os.path.join(self.status, rid + ".log"), "wb") as log:
            os.chmod(log.name, 0o644)
            p = self.run(argv, stdout=subprocess.PIPE, stderr=log, cwd="/")
        if p.returncode != 0:
            raise Refused(f"the build failed (exit {p.returncode}) — its log says why")
        try:
            template = json.loads(p.stdout or b"null")
        except ValueError:
            template = None
        return {"recipe": recipe, "roles": roles, "nodes": len(nodes), "template": (template or {}).get("template")}

    # ---- the loop ----------------------------------------------------------------------------------------------
    def recover(self) -> None:
        """A build running when the builder stopped will not finish: say so instead of leaving it running."""
        for n in os.listdir(self.status):
            if n.endswith(".json") and REQUEST.match(n[:-5]):
                path = os.path.join(self.status, n)
                try:
                    with open(path) as f:
                        st = json.load(f)
                except (OSError, ValueError):
                    continue
                if st.get("state") == "running":
                    st.update(state="failed", error="interrupted: the builder restarted", finished=now())
                    write_json(path, st)

    def serve(self, poll: float = 2.0) -> None:
        self.recover()

        def lane(kind: str):
            while True:
                for rid in self.pending(kind):
                    self.handle(rid)
                time.sleep(poll)
        threading.Thread(target=lane, args=("resolve",), daemon=True).start()     # a lookup never waits for a build
        threading.Thread(target=lane, args=("build",), daemon=True).start()
        while True:
            write_json(os.path.join(self.status, "policy.json"), self.policy())
            time.sleep(HEARTBEAT)


def default_platform() -> str | None:
    """Wheels for glibc 2.35 (Ubuntu 22.04 and newer, Debian 12): the image's own glibc is newer than many hosts'."""
    return "x86_64-manylinux_2_35" if platform.machine() in ("x86_64", "AMD64") else None


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="comfyfarm builder")
    ap.add_argument("--requests", default="/builds/requests")
    ap.add_argument("--status", default="/builds/status")
    ap.add_argument("--art", default=os.environ.get("COMFYFARM_ARTIFACTS", ""))
    ap.add_argument("--share", default="/opt/comfyfarm/share")
    ap.add_argument("--uv", default=os.environ.get("COMFYFARM_UV", "/usr/local/bin/uv"))
    a = ap.parse_args(argv)
    if not a.art or not os.path.isabs(a.art) or not os.path.isdir(a.art):
        ap.error("--art (COMFYFARM_ARTIFACTS): the artifact store, an existing absolute path — the same path the "
                 "workers mount it at")
    if not os.access(a.art, os.W_OK):
        ap.error(f"{a.art} is not writable by this account (uid {os.getuid()}): the builder owns the artifact store")
    sources = [s.strip() for s in os.environ.get("COMFYFARM_BUILD_NODE_SOURCES", "").split(",") if s.strip()]
    plat = os.environ.get("COMFYFARM_BUILD_PLATFORM", default_platform() or "") or None
    b = Builder(a.requests, a.status, a.art, a.share, a.uv if os.path.exists(a.uv) else None, sources, plat)
    print(f"comfyfarm builder: store {a.art}, recipes {', '.join(b.recipes()) or 'none'}, dashboard node packs "
          + (f"from {', '.join(sources)}" if sources else "off"), flush=True)
    b.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
