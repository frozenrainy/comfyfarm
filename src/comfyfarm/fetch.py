"""Artifact copies on a worker host (docs/design/16): standard library only, installed by worker setup into
/opt/comfyfarm/worker/ (root-owned) next to the launcher and the agent.

The router streams the artifacts it reads; the builder signed them. This host keeps its own copy of the store at the
same absolute path and fills it with what its profiles need — checked against the builder's signed list of files, so
a router that is taken over cannot hand on anything else (D-37):

  launcher / agent `fetch <kind/name,…>` (service account)  →  one file per artifact in REQUESTS (it may create files
                                                                there, not list them)
  comfyfarm-fetch.path (root)  →  comfyfarm-fetch.service runs `fetch.py run` as root, which takes the requests and runs
                                  `fetch.py get` as STORE_USER (the store's owner) for each artifact not here yet
  agent `fetch-status <kind/name,…>`  →  present / queued / running (bytes) / failed (why) / missing, per artifact

The same file also makes the list the builder signs (`file_list`), so both sides agree on what "the same" means.

Models (docs/design/17) come the same way, one file each, named `model/<key>`: the builder signs an entry (folder,
file, size, SHA-256); the host fetches the file in ranges — an interrupted copy goes on where it stopped — into the
farm's model store here, checks it whole, then puts it in place.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_USER = "comfyfarm-store"
STORE_HOME = "/var/lib/comfyfarm-store"           # root, 0711
HOME = f"{STORE_HOME}/home"                       # the store account's own
REQUESTS = f"{STORE_HOME}/requests"               # root:<service account>, 0730
STATUS = f"{STORE_HOME}/status"                   # root, 0755
CONFIG = "/etc/comfyfarm/store.env"               # COMFYFARM_STORE_ROUTER, _ART, _MODELS (setup writes it)
SIGNERS = "/etc/comfyfarm/artifact-signers"       # the builder's public key, pinned at setup (root's)
PRINCIPAL, NAMESPACE = "comfyfarm-builder", "comfyfarm-artifact"
KINDS = ("core", "venv", "nodes", "frontend")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
ARTIFACT = re.compile(r"^(core|venv|nodes|frontend|model)/([A-Za-z0-9][A-Za-z0-9._-]{0,62})$")
# the model folders the farm fills: launcher.MODEL_FOLDERS (this file stands alone on a host)
MODEL_FOLDERS = ("checkpoints", "diffusion_models", "text_encoders", "vae", "loras", "clip_vision", "controlnet",
                 "upscale_models", "embeddings", "style_models", "model_patches", "audio_encoders",
                 "latent_upscale_models")
MODEL_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+()-]{0,199}$")
KEY = re.compile(r"^[0-9a-f]{32}$")
FORMAT = 1
CHUNK = 1 << 20
STALE = 600.0          # a copy writes its state every second while bytes come: one silent this long is not running
NOFOLLOW = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)


class Refused(Exception):
    pass


def model_key(folder: str, filename: str, sha256: str) -> str:
    """A model's name for requests and signatures: from where it goes and what it is, never a database id (a farm
    started anew keeps its store)."""
    return hashlib.sha256(f"{folder}/{filename}\0{sha256}".encode()).hexdigest()[:32]


def model_entry(folder: str, filename: str, size: int, sha256: str) -> bytes:
    """What the builder signs for a model: one file, where it goes, its size and SHA-256."""
    if folder not in MODEL_FOLDERS or not MODEL_FILE.match(filename) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise Refused(f"not a model entry: {folder}/{filename}")
    doc = {"format": FORMAT, "type": "model", "key": model_key(folder, filename, sha256), "folder": folder,
           "filename": filename, "size": int(size), "sha256": sha256}
    return (json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def model_doc(entry: bytes, key: str | None = None) -> dict:
    doc = json.loads(entry)
    if (doc.get("format") != FORMAT or doc.get("type") != "model" or doc.get("folder") not in MODEL_FOLDERS
            or not MODEL_FILE.match(str(doc.get("filename", ""))) or not re.fullmatch(r"[0-9a-f]{64}", str(doc.get("sha256")))
            or doc.get("key") != model_key(doc["folder"], doc["filename"], doc["sha256"]) or (key and doc["key"] != key)):
        raise Refused(f"not the signed entry of model {key}")
    return doc


def forget_others(models: str, doc: dict) -> None:
    """Signed entries of other models at the same place (another revision of the file): their file is being replaced,
    and a copy of the same size must not count as theirs."""
    signed = os.path.join(models, "signed", "model")
    for n in os.listdir(signed) if os.path.isdir(signed) else []:
        if n.endswith(".json") and n[:-5] != doc["key"]:
            try:
                with open(os.path.join(signed, n), "rb") as f:
                    other = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(other, dict) and (other.get("folder"), other.get("filename")) == (doc["folder"], doc["filename"]):
                for suffix in (".json", ".json.sig"):
                    try:
                        os.remove(os.path.join(signed, n[:-5] + suffix))
                    except OSError:
                        pass


def model_present(models: str, key: str) -> bool:
    """A copy is here when its signed entry is, and the file of the entry's size."""
    try:
        with open(os.path.join(models, "signed", "model", key + ".json"), "rb") as f:
            doc = model_doc(f.read(), key)
        return os.path.getsize(os.path.join(models, doc["folder"], doc["filename"])) == doc["size"]
    except (OSError, ValueError, Refused):
        return False


def read_env(path: str) -> dict:
    out = {}
    try:
        with open(path) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k and not k.startswith("#"):
                    out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def parse(names) -> list[tuple[str, str]]:
    """'venv/rocm72-1,core/comfyui-0.41.0' or a list of such → [(kind, name)], each checked."""
    items = names.split(",") if isinstance(names, str) else list(names)
    out = []
    for x in items:
        m = ARTIFACT.match(x.strip())
        if not m or (m.group(1) == "model" and not KEY.match(m.group(2))):
            raise Refused(f"not an artifact name: {x!r} (kind/name, kind one of {', '.join(KINDS)}, or model/<key>)")
        if (m.group(1), m.group(2)) not in out:
            out.append((m.group(1), m.group(2)))
    if not out:
        raise Refused("no artifact named")
    return out


# ---- the signed list ----------------------------------------------------------------------------------------------
def file_list(root: str, kind: str, name: str) -> bytes:
    """Every entry under an artifact, in path order: ["d", path, mode] · ["f", path, mode, size, sha256] ·
    ["l", path, target]. READY is left out (whoever installs a copy writes it last). The exact bytes are what the
    builder signs."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if rel == "READY":
                continue
            st = os.lstat(p)
            if os.path.islink(p):
                entries.append(["l", rel, os.readlink(p)])
            elif os.path.isdir(p):
                entries.append(["d", rel, st.st_mode & 0o7777])
            elif os.path.isfile(p):
                h = hashlib.sha256()
                with open(p, "rb") as f:
                    for chunk in iter(lambda: f.read(CHUNK), b""):
                        h.update(chunk)
                entries.append(["f", rel, st.st_mode & 0o7777, st.st_size, h.hexdigest()])
            else:
                raise Refused(f"{kind}/{name}: {rel} is not a file, a directory or a link")
    entries.sort(key=lambda e: e[1])
    doc = {"format": FORMAT, "kind": kind, "name": name, "mode": os.lstat(root).st_mode & 0o7777, "entries": entries}
    return (json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def sign(list_path: str, key: str) -> None:
    """The builder's half: <list>.sig next to the list (ssh-keygen -Y, namespace comfyfarm-artifact)."""
    if os.path.exists(list_path + ".sig"):
        os.remove(list_path + ".sig")
    p = subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", key, "-n", NAMESPACE, list_path],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise Refused(f"signing {list_path} failed: {(p.stderr or p.stdout).strip()[-300:]}")


def verify(listing: bytes, sig: bytes, signers: str = SIGNERS) -> None:
    if not os.path.isfile(signers):
        raise Refused(f"{signers} is missing: this host has no key to check copies with — set it up again")
    with tempfile.TemporaryDirectory() as d:
        sig_path = os.path.join(d, "list.sig")
        with open(sig_path, "wb") as f:
            f.write(sig)
        p = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", signers, "-I", PRINCIPAL, "-n", NAMESPACE,
                            "-s", sig_path], input=listing, capture_output=True)
    if p.returncode != 0:
        raise Refused("the list of files is not signed by this farm's builder — refused (a builder that lost its key "
                      "signs with a new one: set this host up again to pin it)")


# ---- installing a copy --------------------------------------------------------------------------------------------
def _member_path(name: str) -> str:
    rel = name[2:] if name.startswith("./") else name
    rel = rel.rstrip("/")
    if rel in ("", "."):
        return ""
    if rel.startswith("/") or any(part in ("", ".", "..") for part in rel.split("/")):
        raise Refused(f"refused tar entry {name!r}")
    return rel


def install(stream, art: str, kind: str, name: str, listing: bytes, sig: bytes, progress=None) -> str:
    """Unpack a tar stream into <ART>/<kind>/<name> through a temporary directory: only the entries the signed list
    names, each as listed (type, link target, size, SHA-256 — checked while it is written); then the modes, the list
    beside it, the rename and READY last. Anything else and nothing is installed."""
    doc = json.loads(listing)
    if doc.get("format") != FORMAT or doc.get("kind") != kind or doc.get("name") != name:
        raise Refused(f"the signed list is for {doc.get('kind')}/{doc.get('name')}, not {kind}/{name}")
    want = {e[1]: e for e in doc["entries"]}
    final = os.path.join(art, kind, name)
    if os.path.isfile(os.path.join(final, "READY")):
        return final
    os.makedirs(os.path.join(art, kind), exist_ok=True)
    tmp = os.path.join(art, kind, f".incoming-{name}-{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    os.mkdir(tmp, 0o755)
    real_tmp = os.path.realpath(tmp)
    seen, done = set(), 0
    try:
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            for m in tar:
                rel = _member_path(m.name)
                if not rel or rel == "READY":
                    continue
                e = want.get(rel)
                if e is None or rel in seen:
                    raise Refused(f"{kind}/{name}: {rel!r} is not in the signed list" if e is None else
                                  f"{kind}/{name}: {rel!r} twice")
                path = os.path.join(tmp, rel)
                parent = os.path.dirname(path)
                if os.path.realpath(parent) != os.path.join(real_tmp, os.path.dirname(rel)).rstrip("/"):
                    raise Refused(f"{kind}/{name}: {rel!r} is not under a directory of the copy")
                if e[0] == "d" and m.isdir():
                    os.mkdir(path, 0o755)
                elif e[0] == "l" and m.issym() and m.linkname == e[2]:
                    os.symlink(e[2], path)
                elif e[0] == "f" and m.isreg() and m.size == e[3]:
                    h, src = hashlib.sha256(), tar.extractfile(m)
                    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                                           0o644), "wb") as out:
                        for chunk in iter(lambda: src.read(CHUNK), b""):
                            h.update(chunk)
                            out.write(chunk)
                            done += len(chunk)
                            if progress:
                                progress(done)
                    if h.hexdigest() != e[4]:
                        raise Refused(f"{kind}/{name}: {rel} differs from the signed list")
                else:
                    raise Refused(f"{kind}/{name}: {rel!r} is not what the signed list says")
                seen.add(rel)
        if missing := sorted(set(want) - seen):
            raise Refused(f"{kind}/{name}: {len(missing)} entr(y/ies) missing from the stream, e.g. {missing[0]}")
        for e in sorted(want.values(), key=lambda e: e[1].count("/"), reverse=True):   # deepest first: dirs last
            if e[0] != "l":
                os.chmod(os.path.join(tmp, e[1]), e[2])
        signed = os.path.join(art, "signed", kind)
        os.makedirs(signed, exist_ok=True)
        for suffix, data in ((".json", listing), (".json.sig", sig)):
            with os.fdopen(os.open(os.path.join(signed, name + suffix), NOFOLLOW, 0o644), "wb") as f:
                f.write(data)
        if os.path.lexists(final):                     # a copy that never got its READY (interrupted): replaced
            _remove(final)
        os.rename(tmp, final)
        open(os.path.join(final, "READY"), "w").close()
        os.chmod(os.path.join(final, "READY"), 0o444)
        os.chmod(final, doc.get("mode", 0o555))
        return final
    except BaseException:
        _remove(tmp)
        raise


def _remove(path: str) -> None:
    """Read-only trees (published artifacts are sealed) need write permission back before they can go."""
    for d, dirs, _ in os.walk(path):
        for n in [""] + dirs:
            p = os.path.join(d, n) if n else d
            if not os.path.islink(p):
                try:
                    os.chmod(p, 0o755)
                except OSError:
                    pass
    shutil.rmtree(path, ignore_errors=True)


def get(router: str, art: str, kind: str, name: str, signers: str = SIGNERS, out=None) -> str:
    """The store account's half: the signed list, its signature, enough room, then the stream. Progress goes to `out`
    as JSON lines ({"bytes": n, "total": t}) for the root half to show."""
    out = out or sys.stdout
    base = f"{router.rstrip('/')}/api/worker_artifacts/{kind}/{name}"
    try:
        with urllib.request.urlopen(base + "/signed", timeout=60) as r:
            doc = json.load(r)
        listing, sig = doc["list"].encode(), doc["sig"].encode()
    except urllib.error.HTTPError as e:
        raise Refused(f"the router has no signed {kind}/{name} (HTTP {e.code}: {e.read().decode(errors='replace')[:200]})")
    except (OSError, ValueError, KeyError, AttributeError) as e:
        raise Refused(f"cannot read the signed list of {kind}/{name} from {router}: {type(e).__name__}: {e}")
    verify(listing, sig, signers)
    total = sum(e[3] for e in json.loads(listing)["entries"] if e[0] == "f")
    os.makedirs(os.path.join(art, kind), exist_ok=True)
    free = shutil.disk_usage(os.path.join(art, kind)).free
    if free < total + (1 << 30):
        raise Refused(f"{kind}/{name} needs {total / 2**30:.1f} GB, {art} has {free / 2**30:.1f} GB free")
    last = [0.0]

    def progress(n):
        if time.monotonic() - last[0] >= 1.0:
            last[0] = time.monotonic()
            print(json.dumps({"bytes": n, "total": total}), file=out, flush=True)
    try:
        with urllib.request.urlopen(base + "/tar", timeout=300) as r:
            path = install(r, art, kind, name, listing, sig, progress)
    except urllib.error.HTTPError as e:
        raise Refused(f"the router did not send {kind}/{name} (HTTP {e.code})")
    except (OSError, tarfile.TarError) as e:
        raise Refused(f"copying {kind}/{name} broke off: {type(e).__name__}: {e}")
    print(json.dumps({"bytes": total, "total": total}), file=out, flush=True)
    return path


def place_model(models: str, doc: dict, part: str, entry: bytes, sig: bytes) -> str:
    """A whole, checked copy into place: <models>/<folder>/<filename> (read-only), its signed entry beside the others."""
    dest_dir = os.path.join(models, doc["folder"])
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, doc["filename"])
    os.chmod(part, 0o444)
    forget_others(models, doc)
    os.replace(part, dest)                         # an older copy under the same name (another revision) is replaced
    signed = os.path.join(models, "signed", "model")
    os.makedirs(signed, exist_ok=True)
    for suffix, data in ((".json", entry), (".json.sig", sig)):
        with os.fdopen(os.open(os.path.join(signed, doc["key"] + suffix), NOFOLLOW, 0o644), "wb") as f:
            f.write(data)
    return dest


def download(url: str, part: str, size: int, sha256: str | None, headers: dict | None = None, progress=None,
             timeout: float = 300) -> str:
    """GET url into `part`, going on from what an interrupted try left there (Range), hashing everything — the bytes
    already there first. The whole file must have `size` bytes and the SHA-256 `sha256` (when known: a file Hugging
    Face does not keep in LFS has none published); else it is thrown away. Returns the SHA-256."""
    h, have = hashlib.sha256(), 0
    if os.path.exists(part):
        with open(part, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                h.update(chunk)
                have += len(chunk)
        if have > size:
            os.remove(part)
            h, have = hashlib.sha256(), 0
    if have < size:
        req = urllib.request.Request(url, headers={**(headers or {}), **({"Range": f"bytes={have}-"} if have else {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if have and r.status != 206:              # the server sent it all again: start over
                h, have = hashlib.sha256(), 0
                mode = "wb"
            else:
                mode = "ab"
            with open(part, mode) as out:
                for chunk in iter(lambda: r.read(CHUNK), b""):
                    out.write(chunk)
                    h.update(chunk)
                    have += len(chunk)
                    if progress:
                        progress(have)
                    if have > size:
                        break
    if have != size or (sha256 and h.hexdigest() != sha256):
        os.remove(part)
        raise Refused(f"{os.path.basename(part)}: {have} bytes, SHA-256 {h.hexdigest()[:12]}… — not the {size} bytes "
                      f"{(sha256 or '?')[:12]}… expected; thrown away")
    return h.hexdigest()


def get_model(router: str, models: str, key: str, signers: str = SIGNERS, out=None) -> str:
    """The store account's half for a model: the signed entry, its signature, room, then the file in ranges."""
    out = out or sys.stdout
    base = f"{router.rstrip('/')}/api/worker_models/{key}"
    try:
        with urllib.request.urlopen(base + "/signed", timeout=60) as r:
            got = json.load(r)
        entry, sig = got["entry"].encode(), got["sig"].encode()
    except urllib.error.HTTPError as e:
        raise Refused(f"the router has no signed model {key} (HTTP {e.code}: {e.read().decode(errors='replace')[:200]})")
    except (OSError, ValueError, KeyError, AttributeError) as e:
        raise Refused(f"cannot read the signed entry of model {key} from {router}: {type(e).__name__}: {e}")
    verify(entry, sig, signers)
    doc = model_doc(entry, key)
    if model_present(models, key):
        return os.path.join(models, doc["folder"], doc["filename"])
    incoming = os.path.join(models, ".incoming")
    os.makedirs(incoming, exist_ok=True)
    part = os.path.join(incoming, key + ".part")
    have = os.path.getsize(part) if os.path.exists(part) else 0
    free = shutil.disk_usage(incoming).free
    if free < doc["size"] - have + (1 << 30):
        raise Refused(f"{doc['folder']}/{doc['filename']} needs {(doc['size'] - have) / 2**30:.1f} GB more, {models} has "
                      f"{free / 2**30:.1f} GB free")
    last = [0.0]

    def progress(n):
        if time.monotonic() - last[0] >= 1.0:
            last[0] = time.monotonic()
            print(json.dumps({"bytes": n, "total": doc["size"]}), file=out, flush=True)
    try:
        download(base + "/file", part, doc["size"], doc["sha256"], progress=progress)
    except urllib.error.HTTPError as e:
        raise Refused(f"the router did not send model {key} (HTTP {e.code})")
    except OSError as e:                              # the part stays: the next try goes on from there
        raise Refused(f"copying {doc['folder']}/{doc['filename']} broke off at {os.path.getsize(part) if os.path.exists(part) else 0} "
                      f"bytes: {type(e).__name__}: {e} — it goes on from there next time")
    path = place_model(models, doc, part, entry, sig)
    print(json.dumps({"bytes": doc["size"], "total": doc["size"]}), file=out, flush=True)
    return path


# ---- requests and their state (the service account's half) ---------------------------------------------------------
def _key(kind: str, name: str) -> str:
    return f"{kind}.{name}"


def request(names, requests: str | None = None) -> str:
    """One file per artifact in the drop box; one that is already asked for is not asked again."""
    requests = requests or REQUESTS
    if not os.path.isdir(requests):
        raise Refused("copies are not set up on this host (its artifact store is mounted, or setup predates them)")
    asked = []
    for kind, name in parse(names):
        try:
            fd = os.open(os.path.join(requests, _key(kind, name)), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as f:
            f.write(dt.datetime.now(dt.timezone.utc).isoformat() + "\n")
        asked.append(f"{kind}/{name}")
    return "asked for " + ", ".join(asked) if asked else "already asked for"


def here(kind: str, name: str, art: str, models: str) -> bool:
    if kind == "model":
        return bool(models) and model_present(models, name)
    return bool(art) and os.path.isfile(os.path.join(art, kind, name, "READY"))


def status(names, art: str | None = None, requests: str | None = None, status_dir: str | None = None,
           models: str | None = None) -> dict:
    requests, status_dir = requests or REQUESTS, status_dir or STATUS
    cfg = read_env(CONFIG) if art is None or models is None else {}
    art = art if art is not None else cfg.get("COMFYFARM_STORE_ART", "")
    models = models if models is not None else cfg.get("COMFYFARM_STORE_MODELS", "")
    out = {}
    for kind, name in parse(names):
        key = f"{kind}/{name}"
        if here(kind, name, art, models):
            out[key] = {"state": "present"}
            continue
        if kind == "model" and not models:
            out[key] = {"state": "off", "error": "this host keeps no model copies (its models are its own)"}
            continue
        if os.path.exists(os.path.join(requests, _key(kind, name))):
            out[key] = {"state": "queued"}
            continue
        path = os.path.join(status_dir, _key(kind, name) + ".json")
        try:
            with open(path) as f:
                st = json.load(f)
            if st.get("state") == "running" and time.time() - os.path.getmtime(path) > STALE:
                st = {}                  # its service is gone (the host restarted): asked again, a model goes on
        except (OSError, ValueError):
            st = {}
        out[key] = st if st.get("state") in ("running", "failed") else {"state": "missing"}
    return out


def wait(names, art: str, timeout: float, say=print, requests: str | None = None, status_dir: str | None = None,
         poll: float = 2.0) -> None:
    """The launcher's half: ask, then wait until every artifact is here — or one failed, or the time is up."""
    request(names, requests)
    t0, told = time.monotonic(), 0.0
    while True:
        st = status(names, art, requests, status_dir, models="")
        if all(v["state"] == "present" for v in st.values()):
            return
        if failed := {k: v for k, v in st.items() if v["state"] == "failed"}:
            k, v = next(iter(failed.items()))
            raise Refused(f"copying {k} failed: {v.get('error') or 'see /var/lib/comfyfarm-store/status'}")
        if any(v["state"] == "missing" for v in st.values()):
            request(names, requests)            # a copy that stopped without a word (its service went away): again
        if all(v["state"] in ("present", "queued") for v in st.values()) and time.monotonic() - t0 > 120:
            raise Refused("nothing took the request for 2 minutes — is comfyfarm-fetch.path running "
                          "(systemctl status comfyfarm-fetch.path)?")
        if time.monotonic() - told >= 30:
            told = time.monotonic()
            say("waiting for copies: " + ", ".join(
                f"{k} {v['state']}" + (f" {v['bytes'] / 2**30:.1f}/{v['total'] / 2**30:.1f} GB"
                                       if v.get("total") else "") for k, v in st.items() if v["state"] != "present"))
        if time.monotonic() - t0 > timeout:
            raise Refused(f"the copies did not arrive in {timeout / 60:.0f} min")
        time.sleep(poll)


# ---- the root half (comfyfarm-fetch.service) ------------------------------------------------------------------------
def _write_status(status_dir: str, key: str, st: dict) -> None:
    path = os.path.join(status_dir, key + ".json")
    with os.fdopen(os.open(path + ".tmp", NOFOLLOW, 0o644), "w") as f:
        json.dump(st, f)
    os.replace(path + ".tmp", path)


def _asked(requests: str, name: str) -> tuple[str, str] | None:
    """The artifact one entry of the drop box asks for — a plain file with a proper name; anything else the service
    account could put there (a directory, a link, another name) is removed without being looked at."""
    path = os.path.join(requests, name)
    kind, _, rest = name.partition(".")
    if not os.path.islink(path) and os.path.isfile(path) and ((kind in KINDS and NAME.match(rest))
                                                              or (kind == "model" and KEY.match(rest))):
        return kind, rest
    try:
        os.unlink(path) if os.path.islink(path) or not os.path.isdir(path) else shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass
    return None


def _done_with(requests: str, name: str) -> None:
    try:
        os.unlink(os.path.join(requests, name))
    except OSError:
        pass


def run(requests: str | None = None, status_dir: str | None = None, config: str | None = None,
        as_user: str | None = STORE_USER, home: str | None = None, popen=subprocess.Popen) -> list[str]:
    requests, status_dir, home = requests or REQUESTS, status_dir or STATUS, home or HOME
    cfg = read_env(config or CONFIG)
    router, art = cfg.get("COMFYFARM_STORE_ROUTER", ""), cfg.get("COMFYFARM_STORE_ART", "")
    models = cfg.get("COMFYFARM_STORE_MODELS", "")
    done, tried = [], set()
    while new := [n for n in sorted(os.listdir(requests)) if n not in tried]:   # requests that came in meanwhile too
        tried.update(new)
        for entry in new:
            asked = _asked(requests, entry)
            if asked is None:
                continue
            kind, name = asked
            key = _key(kind, name)
            if here(kind, name, art, models) or (kind == "model" and not (models and KEY.match(name))):
                _done_with(requests, entry)                  # here already — or a model on a host without a model store
                continue
            # running before the request goes: a reader in between never sees the last attempt's failure as this one's
            started = dt.datetime.now(dt.timezone.utc).isoformat()
            _write_status(status_dir, key, {"state": "running", "started": started, "bytes": 0, "total": 0})
            _done_with(requests, entry)
            argv = ([sys.executable, os.path.join(HERE, "fetch.py"), "get-model", router, models, name] if kind == "model"
                    else [sys.executable, os.path.join(HERE, "fetch.py"), "get", router, art, kind, name])
            if as_user:
                argv = ["runuser", "-u", as_user, "--", "env", f"HOME={home}", "PATH=/usr/bin:/bin"] + argv
            p = popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd="/")
            for line in p.stdout:
                try:
                    got = json.loads(line)
                    _write_status(status_dir, key, {"state": "running", "started": started, "bytes": got["bytes"],
                                                    "total": got["total"]})
                except (ValueError, KeyError, TypeError):
                    pass
            err = p.stderr.read().strip()
            rc = p.wait()
            finished = dt.datetime.now(dt.timezone.utc).isoformat()
            if rc == 0:
                _write_status(status_dir, key, {"state": "done", "started": started, "finished": finished})
            else:
                _write_status(status_dir, key, {"state": "failed", "started": started, "finished": finished,
                                                "error": (err.splitlines() or [f"exit {rc}"])[-1][-500:]})
            done.append(f"{kind}/{name}")
    return done


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args == ["run"]:
            run()
            return 0
        if len(args) == 5 and args[0] == "get":
            (kind, name), = parse(f"{args[3]}/{args[4]}")
            get(args[1], args[2], kind, name)
            return 0
        if len(args) == 4 and args[0] == "get-model" and KEY.match(args[3]):
            get_model(args[1], args[2], args[3])
            return 0
    except Refused as e:
        print(f"fetch: {e}", file=sys.stderr)
        return 2
    print("usage: fetch.py run (comfyfarm-fetch.service) | fetch.py get <router> <ART> <kind> <name> | "
          "fetch.py get-model <router> <model store> <key>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
