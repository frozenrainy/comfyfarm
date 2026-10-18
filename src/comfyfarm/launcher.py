"""comfy-upgrade/1 launcher (contract §4): fetch this instance's render from the router, validate it, sync artifacts,
then exec ComfyUI. Standard library only and no router imports — copy this one file to a worker and run it from the
service manager:

    COMFY_LAUNCH_ROUTER=http://router:8188 COMFY_LAUNCH_INSTANCE=gpu-1 COMFY_LAUNCH_ARTIFACTS=/srv/art \\
    COMFY_LAUNCH_LOCAL=/srv/comfy COMFY_LAUNCH_PREFIXES=/srv/comfy,/data/models python3 launcher.py [--check]

The launcher is the security boundary of the upgrade path: a compromised router must not be able to make a worker run
anything but stock ComfyUI from published artifacts. Hence allow-lists for flags and environment keys, path prefixes,
and READY files. Exit codes: 0 `--check` passed · 2 refused (one line on stderr) · otherwise execve into ComfyUI.
Platform: posix (contract §9.2: a render for another platform is refused).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request

CONTRACT = "comfy-upgrade/1"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
INSTANCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
KINDS = ("core", "venv", "nodes", "frontend")
KEEP_LOCAL = 3

# ComfyUI 0.41.0 options a render may use: arity (0 flag, 1 one value, "?" optional value, "*"/"+" list) and whether
# the values are paths (then they must lie under the allowed prefixes). Left out on purpose: --base-directory (the
# render sets every directory explicitly), --database-url (arbitrary target), --enable-manager* (installs code at
# runtime), --front-end-version (downloads a frontend at runtime; a frontend comes only as a published artifact via
# --front-end-root), --windows-standalone-build, --quick-test-for-ci, --auto-launch.
OPTIONS: dict[str, tuple[object, bool]] = {
    "--listen": ("?", False), "--port": (1, False), "--tls-keyfile": (1, True), "--tls-certfile": (1, True),
    "--enable-cors-header": ("?", False), "--max-upload-size": (1, False),
    "--extra-model-paths-config": ("+", True), "--output-directory": (1, True), "--temp-directory": (1, True),
    "--input-directory": (1, True), "--previews-directory": (1, True), "--user-directory": (1, True),
    "--models-directory": (1, True), "--front-end-root": (1, True),
    "--disable-auto-launch": (0, False), "--cuda-device": (1, False), "--default-device": (1, False),
    "--cuda-malloc": (0, False), "--disable-cuda-malloc": (0, False), "--force-fp32": (0, False), "--force-fp16": (0, False),
    "--fp32-unet": (0, False), "--fp64-unet": (0, False), "--bf16-unet": (0, False), "--fp16-unet": (0, False),
    "--fp8_e4m3fn-unet": (0, False), "--fp8_e5m2-unet": (0, False), "--fp8_e8m0fnu-unet": (0, False),
    "--fp16-vae": (0, False), "--fp32-vae": (0, False), "--bf16-vae": (0, False), "--cpu-vae": (0, False),
    "--fp8_e4m3fn-text-enc": (0, False), "--fp8_e5m2-text-enc": (0, False), "--fp16-text-enc": (0, False),
    "--fp32-text-enc": (0, False), "--bf16-text-enc": (0, False), "--fp16-intermediates": (0, False),
    "--force-channels-last": (0, False), "--directml": ("?", False), "--oneapi-device-selector": (1, False),
    "--supports-fp8-compute": (0, False), "--enable-triton-backend": (0, False), "--disable-triton-backend": (0, False),
    "--preview-method": (1, False), "--preview-size": (1, False), "--cache-ram": ("*", False), "--cache-classic": (0, False),
    "--cache-lru": (1, False), "--cache-none": (0, False), "--high-ram": (0, False),
    "--use-split-cross-attention": (0, False), "--use-quad-cross-attention": (0, False),
    "--use-pytorch-cross-attention": (0, False), "--use-sage-attention": (0, False), "--use-flash-attention": (0, False),
    "--use-ck-attention": (0, False), "--disable-xformers": (0, False), "--force-upcast-attention": (0, False),
    "--dont-upcast-attention": (0, False), "--gpu-only": (0, False), "--highvram": (0, False), "--lowvram": (0, False),
    "--novram": (0, False), "--cpu": (0, False), "--reserve-vram": (1, False), "--vram-headroom": (1, False),
    "--disable-nvml-pressure": (0, False), "--async-offload": ("?", False), "--disable-async-offload": (0, False),
    "--disable-dynamic-vram": (0, False), "--enable-dynamic-vram": (0, False), "--fast-disk": (0, False),
    "--disable-fast-disk": (0, False), "--disable-cuda-graphs": (0, False), "--disable-comfy-compiler": (0, False),
    "--force-non-blocking": (0, False), "--default-hashing-function": (1, False), "--disable-smart-memory": (0, False),
    "--deterministic": (0, False), "--fast": ("*", False), "--disable-pinned-memory": (0, False),
    "--mmap-torch-files": (0, False), "--disable-mmap": (0, False), "--dont-print-server": (0, False),
    "--disable-metadata": (0, False), "--disable-all-custom-nodes": (0, False), "--whitelist-custom-nodes": ("+", False),
    "--disabled-nodes-config": (1, True), "--disable-partner-nodes": (0, False), "--offline": (0, False),
    "--disable-api-nodes": (0, False), "--multi-user": (0, False), "--verbose": ("*", False), "--log-stdout": (0, False),
    "--enable-compress-response-body": (0, False), "--enable-assets": (0, False), "--disable-assets": (0, False),
    "--enable-asset-hashing": (0, False), "--feature-flag": (1, False),
}
# Environment keys a render may set (exact names or prefixes). Not PATH, PYTHONPATH, LD_*: those run other code. The
# prefixes also cover variables that name plugin libraries, tool binaries or compiled-kernel caches (HSA_TOOLS_LIB,
# TRITON_PTXAS_PATH, MIOPEN_CUSTOM_CACHE_DIR, ...), so values are checked too (env_problem): no paths.
ENV_PREFIXES = ("PYTORCH_", "TORCH_", "CUDA_", "HIP_", "HSA_", "ROCM_", "MIOPEN_", "AMD_", "TRITON_", "NCCL_",
                "COMFYFARM_")        # the worker extension's own settings (COMFYFARM_ALLOW)
ENV_KEYS = {"HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "TOKENIZERS_PARALLELISM", "TZ"}
# Directories ComfyUI lets users write to (uploads, outputs, user files). They must not overlap <ART> or <LOCAL> in
# either direction: <LOCAL> holds the core and node copies ComfyUI imports as code, and an upload's subfolder can reach
# anything below the input directory.
WRITABLE = ("--input-directory", "--output-directory", "--temp-directory", "--user-directory", "--previews-directory")


def nodes_yaml(node_dirs: list[str]) -> str:
    """The extra-model-paths file of a render: custom node directories only (models come from --models-directory)."""
    if not node_dirs:
        return "comfyfarm: {}\n"
    return "comfyfarm:\n  custom_nodes: |\n" + "".join(f"    {d}\n" for d in node_dirs)


class Refused(Exception):
    """The render must not be run. Exit 2."""


def env_key_ok(key: str) -> bool:
    return key in ENV_KEYS or key.startswith(ENV_PREFIXES)


TZ_NAME = re.compile(r"^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$")       # IANA zone name (Asia/Seoul), not a file


def env_problem(key: str, value) -> str | None:
    """Why the render must not set `key=value`, or None. A value must not name a file or directory: no '/' or '\\',
    and no part (split at whitespace , : ; =) starting with '~' or '.'. A bare word can only name something in the
    working directory (the core copy) or be looked up in the system's own search paths, never where users write or
    anywhere outside the artifacts. Two keys need a '/' and get a typed check instead: COMFYFARM_ALLOW (addresses and
    CIDR blocks) and TZ (a zone name; other TZ values follow the general rule)."""
    if not env_key_ok(key):
        return f"environment key not allowed: {key!r}"
    if not isinstance(value, str) or any(ord(ch) < 32 for ch in value):
        return f"environment value of {key} must be one line of text"
    if key == "COMFYFARM_ALLOW":
        for item in value.replace(";", ",").split(","):
            try:
                if item.strip():
                    ipaddress.ip_network(item.strip(), strict=False)
            except ValueError:
                return f"COMFYFARM_ALLOW: {item.strip()!r} is not an address or CIDR block"
        return None
    if key == "TZ" and TZ_NAME.match(value):
        return None
    if "/" in value or "\\" in value or any(t.startswith(("~", ".")) for t in re.split(r"[\s,:;=]+", value)):
        return f"environment value of {key} names a path: {value!r}"
    return None


def _norm(path: str) -> str:
    if not isinstance(path, str) or "\0" in path or not os.path.isabs(path):
        raise Refused(f"not an absolute path: {path!r}")
    return os.path.normpath(path)


def under(path: str, roots) -> bool:
    p = _norm(path)
    return any(p == os.path.normpath(r) or p.startswith(os.path.normpath(r).rstrip("/") + "/") for r in roots)


def validate(render: dict, *, instance: str, art: str, local: str, prefixes: list[str], check_files: bool = True) -> None:
    """Every rule of contract §4. Raises Refused with the first violation. `check_files=False` skips what needs the
    worker's disk (READY files) — the router uses that to refuse an assignment the launcher would refuse."""
    if not isinstance(render, dict):
        raise Refused("render is not an object")
    major = str(render.get("contract", "")).split(".")[0]
    if major != CONTRACT:
        raise Refused(f"contract {render.get('contract')!r} is not {CONTRACT}")
    if render.get("instance") != instance:
        raise Refused(f"render is for {render.get('instance')!r}, not {instance!r}")
    if render.get("platform", "posix") != "posix":
        raise Refused(f"platform {render.get('platform')!r} is not implemented by this launcher")
    art, local = _norm(art), _norm(local)
    roots = [local] + [_norm(p) for p in prefixes]      # writable/readable places; the artifact store is read-only
    argv = render.get("argv")
    if not isinstance(argv, list) or len(argv) < 2 or not all(isinstance(a, str) for a in argv):
        raise Refused("argv must be a list of strings")
    m = re.fullmatch(re.escape(art) + r"/venv/([^/]+)/bin/python", _norm(argv[0]))
    if not m or not NAME.match(m.group(1)):
        raise Refused(f"argv[0] must be {art}/venv/<name>/bin/python")
    venv = m.group(1)
    m = re.fullmatch(re.escape(local) + r"/core/([^/]+)/main\.py", _norm(argv[1]))
    if not m or not NAME.match(m.group(1)):
        raise Refused(f"argv[1] must be {local}/core/<name>/main.py")
    core = m.group(1)
    opts: list[tuple[str, list[str]]] = []
    i = 2
    while i < len(argv):
        opt = argv[i]
        if opt not in OPTIONS:
            raise Refused(f"option not allowed: {opt!r}")
        arity, is_path = OPTIONS[opt]
        i += 1
        values = []
        if arity == 1:
            if i >= len(argv):
                raise Refused(f"{opt} needs a value")
            values, i = [argv[i]], i + 1
        elif arity in ("?", "*", "+"):
            while i < len(argv) and not argv[i].startswith("--"):
                values.append(argv[i]); i += 1
            if arity == "+" and not values:
                raise Refused(f"{opt} needs a value")
            if arity == "?" and len(values) > 1:
                raise Refused(f"{opt} takes at most one value")
        for v in values:
            if is_path and opt != "--front-end-root" and (not under(v, roots) or under(v, [art])):   # that one: below
                raise Refused(f"{opt} {v!r} is outside the allowed prefixes")
            if opt in WRITABLE and (under(v, [art, local]) or under(art, [v]) or under(local, [v])):
                raise Refused(f"{opt} {v!r} must lie outside <ART> and <LOCAL> (users write there; code is imported from there)")
        opts.append((opt, values))
    env = render.get("env", {})
    if not isinstance(env, dict):
        raise Refused("env must be an object")
    for k, v in env.items():
        if why := env_problem(k, v):
            raise Refused(why)

    for key, typ in (("sync", list), ("files", dict), ("mkdirs", list), ("require", list)):
        if not isinstance(render.get(key, typ()), typ):
            raise Refused(f"{key} must be {'a list' if typ is list else 'an object'}")
    synced: dict[str, list[str]] = {"core": [], "nodes": [], "frontend": []}
    for e in render.get("sync", []):
        if not isinstance(e, dict):
            raise Refused(f"bad sync entry {e!r}")
        kind, name = e.get("kind"), e.get("name", "")
        if kind not in synced or not isinstance(name, str) or not NAME.match(name):
            raise Refused(f"bad sync entry {e!r}")
        if _norm(e.get("src", "")) != f"{art}/{kind}/{name}" or _norm(e.get("dst", "")) != f"{local}/{kind}/{name}":
            raise Refused(f"sync {kind}/{name} must copy <ART>/{kind}/{name} to <LOCAL>/{kind}/{name}")
        synced[kind].append(name)
    if synced["core"] != [core]:
        raise Refused(f"the core in argv ({core}) must be the one synced")
    if _norm(render.get("cwd", "")) != f"{local}/core/{core}":
        raise Refused("cwd must be the core directory")
    needed = {f"{art}/core/{core}/READY", f"{art}/venv/{venv}/READY"} | {f"{art}/nodes/{n}/READY" for n in synced["nodes"]}

    # Only the render's own yaml may be passed, and its content must be exactly what the synced node artifacts give:
    # a yaml pointing custom_nodes at, say, the input directory would load whatever a user uploaded there.
    run_dir = f"{local}/run/{instance}"
    files = render.get("files") or {}
    yamls = [v for opt, vals in opts if opt == "--extra-model-paths-config" for v in vals]
    if sorted(_norm(y) for y in yamls) != sorted(_norm(f) for f in files):
        raise Refused("--extra-model-paths-config must name exactly the render's own files")
    for path, content in files.items():
        if not re.fullmatch(re.escape(run_dir) + r"/extra_model_paths\.[0-9a-f]{12}\.yaml", _norm(path)):
            raise Refused(f"files may only be {run_dir}/extra_model_paths.<rev>.yaml: {path!r}")
        if not isinstance(content, str) or content != nodes_yaml([f"{local}/nodes/{n}" for n in synced["nodes"]]):
            raise Refused("extra model paths content is not the synced node artifacts")
    for opt, vals in opts:
        if opt == "--front-end-root":
            m = re.fullmatch(re.escape(art) + r"/frontend/([^/]+)", _norm(vals[0]))
            if not m or not NAME.match(m.group(1)):
                raise Refused("--front-end-root must be a published frontend artifact")
            needed.add(f"{art}/frontend/{m.group(1)}/READY")
    for d in render.get("mkdirs", []):
        if not under(d, roots) or under(d, [art]):                        # never inside the artifact store
            raise Refused(f"mkdir outside the allowed prefixes: {d!r}")
    require = {_norm(r) for r in render.get("require", [])}
    if missing := needed - require:
        raise Refused(f"require is missing {sorted(missing)}")
    for r in require:
        m = re.fullmatch(re.escape(art) + r"/(core|venv|nodes|frontend)/([^/]+)/READY", r)
        if not m or not NAME.match(m.group(2)):
            raise Refused(f"require must name <ART>/<kind>/<name>/READY: {r!r}")
        if check_files and not os.path.isfile(r):
            raise Refused(f"artifact not ready: {r}")
    if not re.fullmatch(r"[0-9a-f]{12}", str(render.get("rev", ""))):
        raise Refused("rev must be 12 hex characters")


# ---- local copies -------------------------------------------------------------------------------------------------
def tree_lines(root: str) -> list[bytes]:
    """The lines tree_sha256 hashes, in hash order (exposed for the contract's test vectors)."""
    return _tree_lines(root)


def _collect(root: str) -> list[bytes]:
    """Contract §2 (v1.2): 'f <rel> <size>' / 'l <rel> <target>' lines, full-line bytes sorted ascending, joined by
    newline between lines, SHA-256. Top-level manifest.json and READY are left out (the manifest carries this hash;
    READY is created after it). A change-detection key: the launcher compares manifest values as opaque strings and
    never recomputes this."""
    items = []

    def link(p: str, rel: str) -> str:
        target = os.readlink(p)
        if "\n" in target:                       # would end the line early: two trees could give the same text
            raise ValueError(f"newline in a symlink target: {rel!r}")
        return f"l {rel} {target}"
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames:
            p = os.path.join(dirpath, n)
            if os.path.islink(p):
                rel = os.path.relpath(p, root)
                if "\n" in rel:
                    raise ValueError(f"newline in a file name: {rel!r}")
                items.append(link(p, rel))
        for n in filenames:
            p = os.path.join(dirpath, n)
            rel = os.path.relpath(p, root)
            if rel in ("READY", "manifest.json"):
                continue
            if "\n" in rel:
                raise ValueError(f"newline in a file name: {rel!r}")
            if os.path.islink(p):
                items.append(link(p, rel))
            elif os.path.isfile(p):
                items.append(f"f {rel} {os.path.getsize(p)}")
            else:
                raise ValueError(f"not a regular file or symlink: {rel}")     # sockets, FIFOs, devices (contract §2)
    try:
        return sorted(x.encode("utf-8") for x in items)                      # full-line bytes, ascending
    except UnicodeEncodeError:                                                 # a name that is not valid UTF-8
        raise ValueError("file name is not valid UTF-8")


def _tree_lines(root: str) -> list[bytes]:
    return _collect(root)


def tree_sha256(root: str) -> str:
    return hashlib.sha256(b"\n".join(tree_lines(root))).hexdigest()


def manifest_hash(path: str) -> str | None:
    try:
        with open(os.path.join(path, "manifest.json")) as f:
            return json.load(f).get("tree_sha256")
    except (OSError, ValueError):
        return None


def sync(entry: dict) -> None:
    """Copy src → dst unless dst carries the same tree_sha256; via a temp dir and rename, never in place. Either way
    the copy's mtime is set to now: that is its last use for prune() (copytree would carry the publish time over)."""
    src, dst = entry["src"], entry["dst"]
    want = manifest_hash(src)
    if not (want and manifest_hash(dst) == want):
        tmp = f"{dst}.tmp-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copytree(src, tmp, symlinks=True)
        owner_writable(tmp)        # published artifacts are read-only; the local copy must stay replaceable and prunable
        if os.path.exists(dst):
            old = f"{dst}.old-{os.getpid()}"
            os.rename(dst, old)
            os.rename(tmp, dst)
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.rename(tmp, dst)
    os.utime(dst)


def owner_writable(root: str) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        for n in [""] + dirnames + filenames:
            p = os.path.join(dirpath, n) if n else dirpath
            if not os.path.islink(p):
                os.chmod(p, os.lstat(p).st_mode | 0o200)


IN_FLIGHT = re.compile(r"\.(tmp|old)-\d+$")         # sync()'s work directories (maybe another launcher's, mid-copy)


def referenced(local: str) -> dict[str, set[str]] | None:
    """Artifact names in the current render of every instance under <LOCAL> (`run/<inst>/render.json`, saved by
    fetch() before anything is synced): several instances may share one <LOCAL>. None if one cannot be read."""
    out: dict[str, set[str]] = {k: set() for k in KINDS}
    run = os.path.join(local, "run")
    try:
        names = os.listdir(run)
    except FileNotFoundError:
        return out
    except OSError:
        return None
    for inst in names:
        path = os.path.join(run, inst, "render.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                for e in json.load(f).get("sync") or []:
                    out[e["kind"]].add(e["name"])
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            return None
    return out


def prune(local: str, kind: str, keep_names: set[str]) -> None:
    """Keep the KEEP_LOCAL most recently used copies per kind (sync() stamps the use), plus every copy that any
    instance under <LOCAL> currently runs. If an instance's run state cannot be read, nothing is removed."""
    refs = referenced(local)
    if refs is None:
        return
    keep = set(keep_names) | refs.get(kind, set())
    base = os.path.join(local, kind)
    try:
        entries = [e for e in os.scandir(base) if e.is_dir(follow_symlinks=False) and NAME.match(e.name)
                   and not IN_FLIGHT.search(e.name)]
    except OSError:
        return
    entries.sort(key=lambda e: e.stat(follow_symlinks=False).st_mtime, reverse=True)
    for e in entries[KEEP_LOCAL:]:
        if e.name not in keep:
            shutil.rmtree(e.path, ignore_errors=True)


# ---- render fetch -------------------------------------------------------------------------------------------------
def fetch(router: str, instance: str, local: str) -> dict:
    """200 → use and save · 403/404 → refuse (never fall back to a cache: the router said no) · anything else →
    the last saved render (the router may be down; the farm must still start)."""
    saved = os.path.join(local, "run", instance, "render.json")
    url = f"{router.rstrip('/')}/api/worker_profile?instance={instance}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            body = r.read()
        render = json.loads(body)
        os.makedirs(os.path.dirname(saved), exist_ok=True)
        with open(saved + ".tmp", "wb") as f:
            f.write(body)
        os.replace(saved + ".tmp", saved)
        return render
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            raise Refused(f"router answered {e.code} for instance {instance!r}")
        reason = f"HTTP {e.code}"
    except (OSError, ValueError) as e:
        reason = type(e).__name__
    try:
        with open(saved) as f:
            render = json.load(f)
    except (OSError, ValueError):
        raise Refused(f"router unavailable ({reason}) and no saved render")
    print(f"comfyfarm-launcher: router unavailable ({reason}); starting saved render {render.get('rev')}", file=sys.stderr)
    return render


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    env = os.environ
    try:
        cfg = {k: env.get(f"COMFY_LAUNCH_{k.upper()}") for k in ("router", "instance", "artifacts", "local")}
        if not all(cfg.values()):
            raise Refused("set COMFY_LAUNCH_ROUTER, _INSTANCE, _ARTIFACTS and _LOCAL")
        if not INSTANCE.match(cfg["instance"]):
            raise Refused("bad instance name")
        prefixes = [p for p in env.get("COMFY_LAUNCH_PREFIXES", "").split(",") if p]
        render = fetch(cfg["router"], cfg["instance"], cfg["local"])
        try:
            validate(render, instance=cfg["instance"], art=cfg["artifacts"], local=cfg["local"], prefixes=prefixes)
        except (TypeError, AttributeError, KeyError, ValueError) as e:     # any malformed render is a refusal, not a crash
            raise Refused(f"malformed render ({type(e).__name__}: {e})".replace("\n", " "))
        if "--check" in args:
            print(f"ok {render['rev']}")
            return 0
        run_dir = os.path.join(cfg["local"], "run", cfg["instance"])
        for d in render.get("mkdirs", []):
            os.makedirs(d, exist_ok=True)
        os.makedirs(run_dir, exist_ok=True)
        for e in render.get("sync", []):
            sync(e)
        for kind in ("core", "nodes", "frontend"):
            prune(cfg["local"], kind, {e["name"] for e in render.get("sync", []) if e["kind"] == kind})
        for n in os.listdir(run_dir):               # old yaml files of earlier revs
            if n.startswith("extra_model_paths.") and n.endswith(".yaml"):
                os.remove(os.path.join(run_dir, n))
        for path, content in (render.get("files") or {}).items():
            with open(path, "w") as f:
                f.write(content)
    except Refused as e:
        print(f"comfyfarm-launcher: refused: {e}", file=sys.stderr)
        return 2
    child_env = {k: v for k, v in env.items() if not k.startswith("COMFY_LAUNCH_")}
    child_env.update(render.get("env", {}))
    os.chdir(render["cwd"])
    os.execve(render["argv"][0], render["argv"], child_env)     # PID kept for the service manager
    return 1                                                    # not reached


if __name__ == "__main__":
    sys.exit(main())
