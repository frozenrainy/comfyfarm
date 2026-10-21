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
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time

from . import fetch, publish

REQUEST = re.compile(r"^[0-9]{10,20}-[0-9a-f]{8}$")
REF = re.compile(r"^(?!-)[A-Za-z0-9][A-Za-z0-9._/+-]{0,120}$")
HTTPS = re.compile(r"^https://[A-Za-z0-9][A-Za-z0-9._~:/@-]*$")
NODE_KEYS = {"name", "repo", "commit", "ui", "requirements_file", "constraints"}
HF_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
HF_PATH = re.compile(r"^(?!/)(?!.*(^|/)\.\.?(/|$))[A-Za-z0-9._ +()/-]{1,300}$")
SAFE_MODELS = (".safetensors", ".sft", ".gguf")     # load as data; .ckpt/.pt/.pth/.bin can run code (docs/design/17)
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
                 platform_tag: str | None = None, runner=subprocess.run, key: str | None = None,
                 models: str | None = None, hf_endpoint: str = "https://huggingface.co", hf_token_file: str | None = None,
                 allow_pickle: bool = False):
        self.requests, self.status, self.art, self.share, self.uv = requests, status, art, share, uv
        self.models = models                     # the farm's model store (docs/design/17); None: no models here
        self.hf_endpoint, self.hf_token_file, self.allow_pickle = hf_endpoint.rstrip("/"), hf_token_file, allow_pickle
        self.node_sources = [s for s in node_sources if s]
        self.platform = platform_tag
        self.run = runner
        self.key = key                     # the signing key of the copies GPU hosts make (docs/design/16)
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
                "platform": self.platform, "signing_key": self.public_key(), "models": bool(self.models),
                "model_formats": None if self.allow_pickle else list(SAFE_MODELS),
                "hf_token": bool(self.hf_token_file and os.path.isfile(self.hf_token_file)),
                "alive": now(), "pid": os.getpid()}

    # ---- signatures (docs/design/16) ---------------------------------------------------------------------------
    def ensure_key(self) -> None:
        """An SSH key of its own, made once in its home: what it signs, GPU hosts accept as this farm's (setup pins
        the public half). Neither the router nor anything else holds it."""
        if not self.key or os.path.exists(self.key):
            return
        os.makedirs(os.path.dirname(self.key), mode=0o700, exist_ok=True)
        p = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", fetch.PRINCIPAL, "-f", self.key],
                           capture_output=True, text=True)
        if p.returncode != 0:                        # builds go on; GPU hosts cannot keep copies until it has one
            say(f"no signing key: ssh-keygen said {(p.stderr or p.stdout).strip()!r} — copies on GPU hosts are off")
            return
        say(f"made the signing key {self.key}.pub")

    def public_key(self) -> str | None:
        try:
            with open(self.key + ".pub") as f:
                parts = f.read().split()
            return f"{parts[0]} {parts[1]}" if len(parts) >= 2 else None
        except (OSError, TypeError):
            return None

    def signed_by_me(self, lst: str, signers: str) -> bool:
        try:
            with open(lst, "rb") as f, open(lst + ".sig", "rb") as g:
                fetch.verify(f.read(), g.read(), signers)
            return True
        except (OSError, fetch.Refused):
            return False

    def sign_store(self) -> int:
        """Sign every published artifact not signed by this key — new builds, what the store held before signatures,
        and what an earlier key signed (a builder whose home was deleted makes a new key while the store stays: hosts
        that pin the new one would refuse every copy): <ART>/signed/<kind>/<name>.json (the list of its files) and
        .json.sig. A list already there is signed again as it is (artifacts never change); a missing one is made."""
        pub = self.public_key()
        if not self.key or not os.path.exists(self.key) or not pub:
            return 0
        signers = os.path.join(os.path.dirname(self.key), "allowed_signers")
        with open(signers, "w") as f:
            f.write(f'{fetch.PRINCIPAL} namespaces="{fetch.NAMESPACE}" {pub}\n')
        n = 0
        mdir = os.path.join(self.models, "signed", "model") if self.models else None
        for name in sorted(os.listdir(mdir)) if mdir and os.path.isdir(mdir) else []:
            lst = os.path.join(mdir, name)
            if name.endswith(".json") and not (os.path.exists(lst + ".sig") and self.signed_by_me(lst, signers)):
                try:
                    fetch.sign(lst, self.key)            # a model entry another key signed: this key signs it again
                    os.chmod(lst + ".sig", 0o644)
                    n += 1
                except (OSError, fetch.Refused) as e:
                    say(f"could not sign model entry {name}: {e}")
        for kind in fetch.KINDS:
            d = os.path.join(self.art, kind)
            for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
                lst = os.path.join(self.art, "signed", kind, name + ".json")
                if not fetch.NAME.match(name) or not os.path.isfile(os.path.join(d, name, "READY")):
                    continue
                if os.path.exists(lst + ".sig") and self.signed_by_me(lst, signers):
                    continue
                try:
                    os.makedirs(os.path.dirname(lst), exist_ok=True)
                    if not os.path.isfile(lst):
                        with open(lst + ".tmp", "wb") as f:
                            f.write(fetch.file_list(os.path.join(d, name), kind, name))
                        os.chmod(lst + ".tmp", 0o644)
                        os.replace(lst + ".tmp", lst)
                    fetch.sign(lst, self.key)
                    os.chmod(lst + ".sig", 0o644)
                    n += 1
                except (OSError, fetch.Refused) as e:
                    say(f"could not sign {kind}/{name}: {e}")
        if n:
            say(f"signed {n} artifact(s) for copies on GPU hosts")
        return n

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
            if st["kind"] == "build":                    # the dashboard says what is building while it runs
                st["recipe"] = str(req.get("recipe"))[:100]
            if st["kind"] == "model":
                st["model"] = f"{req.get('folder')}/{req.get('filename')}"[:300]
            write_json(os.path.join(self.status, rid + ".json"), st)
            say(f"{rid}: {req.get('kind')} " + (f"{req.get('recipe')} ({','.join(req.get('roles') or publish.ROLES)}, "
                                               f"{len(req.get('nodes') or [])} node pack(s))" if req.get("kind") == "build"
                                               else f"{req.get('repo')} {req.get('path', '')} {req.get('ref') or 'HEAD'}".replace("  ", " ")))
            if req.get("kind") == "resolve":
                st.update(self.resolve(req))
            elif req.get("kind") == "build":
                st.update(self.build(rid, req))
            elif req.get("kind") == "model":
                st.update(self.model(rid, req, st))
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
        with self.lock:
            self.sign_store()                        # what it just published, before anyone asks for a copy
        try:
            template = json.loads(p.stdout or b"null")
        except ValueError:
            template = None
        return {"recipe": recipe, "roles": roles, "nodes": len(nodes), "template": (template or {}).get("template")}

    # ---- models (docs/design/17) -------------------------------------------------------------------------------
    def hf_headers(self) -> dict:
        try:
            with open(self.hf_token_file) as f:
                token = f.read().strip()
        except (OSError, TypeError):
            return {}
        return {"Authorization": f"Bearer {token}"} if token else {}

    @staticmethod
    def gated(repo: str) -> Refused:
        return Refused(f"{repo} is gated or private: accept its terms on Hugging Face with the account whose token is "
                       "in the builder's hf_token file (docs/guide/models.md)")

    def hf_file(self, repo: str, path: str, ref: str) -> dict:
        """The commit a branch or tag names now, and the file's size and SHA-256 as Hugging Face publishes them."""
        import urllib.error
        import urllib.parse
        import urllib.request
        url = f"{self.hf_endpoint}/api/models/{repo}/revision/{urllib.parse.quote(ref, safe='')}?blobs=true"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=self.hf_headers()), timeout=60) as r:
                info = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise self.gated(repo)
            raise Refused(f"{repo}@{ref}: Hugging Face answered {e.code}")
        except (OSError, ValueError) as e:
            raise Refused(f"cannot reach Hugging Face ({self.hf_endpoint}): {type(e).__name__}: {e}")
        commit = info.get("sha")
        f = next((s for s in info.get("siblings") or [] if s.get("rfilename") == path), None)
        if not (isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit)) or f is None:
            raise Refused(f"no file {path!r} in {repo}@{ref}")
        lfs = f.get("lfs") or {}
        size = lfs.get("size", f.get("size"))
        if not isinstance(size, int):
            raise Refused(f"Hugging Face gave no size for {repo}/{path}")
        sha = lfs.get("sha256") if isinstance(lfs.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", lfs["sha256"]) else None
        return {"commit": commit, "size": size, "sha256": sha}

    def model(self, rid: str, req: dict, st: dict) -> dict:
        """A model from Hugging Face into the store: pinned to the commit its ref names, downloaded in ranges (an
        interrupted one goes on), checked against the published SHA-256, put in place read-only, its entry signed."""
        if not self.models:
            raise Refused("this builder has no model store (COMFYFARM_MODELS)")
        repo, path, ref = req.get("repo"), req.get("path"), (req.get("ref") or "main")
        folder, filename = req.get("folder"), req.get("filename")
        if not isinstance(repo, str) or not HF_REPO.match(repo):
            raise Refused(f"not a Hugging Face model repository: {repo!r}")
        if not isinstance(path, str) or not HF_PATH.match(path) or not isinstance(ref, str) or not REF.match(ref):
            raise Refused(f"bad file or revision: {path!r}@{ref!r}")
        if folder not in fetch.MODEL_FOLDERS or not isinstance(filename, str) or not fetch.MODEL_FILE.match(filename):
            raise Refused(f"bad place for a model: {folder!r}/{filename!r}")
        if not self.allow_pickle and not (filename.lower().endswith(SAFE_MODELS) and path.lower().endswith(SAFE_MODELS)):
            raise Refused(f"{filename}: only {', '.join(SAFE_MODELS)} — other formats can run code when ComfyUI loads them "
                          "(the builder's COMFYFARM_MODELS_ALLOW_PICKLE=1 allows them)")
        hf = self.hf_file(repo, path, ref)
        if hf["sha256"] and fetch.model_present(self.models, key := fetch.model_key(folder, filename, hf["sha256"])):
            lst = os.path.join(self.models, "signed", "model", key + ".json")
            if self.key and os.path.exists(self.key):  # in the store already (added again, a database started anew):
                fetch.sign(lst, self.key)               # signed again by this key, nothing downloaded
                os.chmod(lst + ".sig", 0o644)
            return {"key": key, "sha256": hf["sha256"], "size": hf["size"], "revision": hf["commit"], "folder": folder,
                    "filename": filename, "bytes": hf["size"], "total": hf["size"]}
        incoming = os.path.join(self.models, ".incoming")
        os.makedirs(incoming, exist_ok=True)
        part = os.path.join(incoming, f"{hf['commit'][:12]}-{hashlib.sha256(path.encode()).hexdigest()[:12]}.part")
        last = [0.0]

        def progress(n):
            if time.monotonic() - last[0] >= 2.0:
                last[0] = time.monotonic()
                write_json(os.path.join(self.status, rid + ".json"), {**st, "bytes": n, "total": hf["size"]})
        import urllib.error
        import urllib.parse
        url = f"{self.hf_endpoint}/{repo}/resolve/{hf['commit']}/{urllib.parse.quote(path)}"
        try:
            sha = fetch.download(url, part, hf["size"], hf["sha256"], headers=self.hf_headers(), progress=progress,
                                 timeout=600)
        except urllib.error.HTTPError as e:      # a gated repository shows its files to anyone, and serves them not
            if e.code in (401, 403):
                raise self.gated(repo)
            raise Refused(f"downloading {repo}/{path}: Hugging Face answered {e.code} — Retry goes on from there")
        except OSError as e:
            raise Refused(f"downloading {repo}/{path} broke off: {type(e).__name__}: {e} — Retry goes on from there")
        except fetch.Refused as e:
            raise Refused(str(e))
        entry = fetch.model_entry(folder, filename, hf["size"], sha)
        doc = fetch.model_doc(entry)
        dest_dir = os.path.join(self.models, folder)
        os.makedirs(dest_dir, exist_ok=True)
        os.chmod(part, 0o444)
        fetch.forget_others(self.models, doc)
        os.replace(part, os.path.join(dest_dir, filename))
        lst = os.path.join(self.models, "signed", "model", doc["key"] + ".json")
        os.makedirs(os.path.dirname(lst), exist_ok=True)
        with open(lst + ".tmp", "wb") as f:
            f.write(entry)
        os.chmod(lst + ".tmp", 0o644)
        os.replace(lst + ".tmp", lst)
        if self.key and os.path.exists(self.key):
            fetch.sign(lst, self.key)
            os.chmod(lst + ".sig", 0o644)
        return {"key": doc["key"], "sha256": sha, "size": hf["size"], "revision": hf["commit"], "folder": folder,
                "filename": filename, "bytes": hf["size"], "total": hf["size"]}

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
        self.ensure_key()

        def sign_old():
            with self.lock:                          # what the store held before signatures; a build waits for it
                self.sign_store()
        threading.Thread(target=sign_old, daemon=True).start()

        def lane(kind: str):
            while True:
                for rid in self.pending(kind):
                    self.handle(rid)
                time.sleep(poll)
        threading.Thread(target=lane, args=("resolve",), daemon=True).start()     # a lookup never waits for a build
        threading.Thread(target=lane, args=("build",), daemon=True).start()
        threading.Thread(target=lane, args=("model",), daemon=True).start()       # nor a build for a 30 GB download
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
    ap.add_argument("--models", default=os.environ.get("COMFYFARM_MODELS", ""),
                    help="the farm's model store (docs/design/17), owned by this account; empty: no models")
    a = ap.parse_args(argv)
    if not a.art or not os.path.isabs(a.art) or not os.path.isdir(a.art):
        ap.error("--art (COMFYFARM_ARTIFACTS): the artifact store, an existing absolute path — the same path the "
                 "workers mount it at")
    if not os.access(a.art, os.W_OK):
        ap.error(f"{a.art} is not writable by this account (uid {os.getuid()}): the builder owns the artifact store")
    sources = [s.strip() for s in os.environ.get("COMFYFARM_BUILD_NODE_SOURCES", "").split(",") if s.strip()]
    plat = os.environ.get("COMFYFARM_BUILD_PLATFORM", default_platform() or "") or None
    key = os.path.join(os.environ.get("HOME") or "/var/lib/comfyfarm-builder", "signing", "artifacts")
    models = a.models if a.models and os.path.isdir(a.models) and os.access(a.models, os.W_OK) else None
    if a.models and not models:
        print(f"comfyfarm builder: {a.models} is not a writable directory — no models from the dashboard", flush=True)
    b = Builder(a.requests, a.status, a.art, a.share, a.uv if os.path.exists(a.uv) else None, sources, plat, key=key,
                models=models, hf_endpoint=os.environ.get("COMFYFARM_HF_ENDPOINT") or "https://huggingface.co",
                hf_token_file=os.environ.get("COMFYFARM_HF_TOKEN_FILE") or None,
                allow_pickle=os.environ.get("COMFYFARM_MODELS_ALLOW_PICKLE") == "1")
    print(f"comfyfarm builder: store {a.art}, recipes {', '.join(b.recipes()) or 'none'}, dashboard node packs "
          + (f"from {', '.join(sources)}" if sources else "off") + f", models {models or 'off'}", flush=True)
    b.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
