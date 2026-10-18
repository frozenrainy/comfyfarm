"""Launcher (contract §4) and render (§3): the security boundary of the upgrade path. Every rule gets a deliberately
wrong render that must be refused — a validator that passes everything looks exactly like one that works."""
import copy
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from comfyfarm import launcher
from comfyfarm.rollout import render as rd
from conftest import free_port


def make_world(tmp_path, nodes=("base-nodes",), frontend=None):
    art, local = tmp_path / "art", tmp_path / "local"
    paths = {"art": str(art), "local": str(local), "models": str(tmp_path / "models"), "input": str(tmp_path / "io/in"),
             "output": str(tmp_path / "io/out"), "user": str(tmp_path / "io/user"), "temp": str(tmp_path / "io/tmp")}
    inst = {"instance": "gpu-1", "role": "gpu", "platform": "posix", "listen": "0.0.0.0", "port": 8188, "paths": paths}
    profile = {"name": "stable", "core": "comfyui-0.41.0", "venv": "rocm72-a", "nodes": list(nodes), "frontend": frontend,
               "flags": ["--preview-method", "auto", "--reserve-vram", "1"], "env": {"PYTORCH_TUNABLEOP_ENABLED": "1"}}
    for kind, name in [("core", profile["core"]), ("venv", profile["venv"])] + [("nodes", n) for n in nodes] + \
            ([("frontend", frontend)] if frontend else []):
        d = art / kind / name
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"contract": "comfy-upgrade/1", "kind": kind, "name": name,
                                                     "tree_sha256": "x" + name}))
        (d / "READY").write_text("")
    (art / "core" / profile["core"] / "main.py").write_text("print('comfy')\n")
    return profile, inst


def validate(render, inst, check_files=True, prefixes=None):
    p = inst["paths"]
    launcher.validate(render, instance=inst["instance"], art=p["art"], local=p["local"],
                      prefixes=prefixes if prefixes is not None else [p[k] for k in ("models", "input", "output", "user", "temp")],
                      check_files=check_files)


def test_render_shape_rev_and_badge(tmp_path):
    profile, inst = make_world(tmp_path)
    r = rd.build(profile, inst)
    validate(r, inst)
    yaml = [a for a in r["argv"] if a.endswith(".yaml")][0]
    assert yaml.endswith(f"/run/gpu-1/extra_model_paths.{r['rev']}.yaml") and list(r["files"]) == [yaml]
    assert r["argv"][0].endswith("/art/venv/rocm72-a/bin/python") and r["argv"][1].endswith("/local/core/comfyui-0.41.0/main.py")
    assert "platform" not in r                                           # posix renders stay v1-identical
    # rev depends on the content, and a venv-only change shows in argv[1:] through the yaml name (§5)
    r2 = rd.build(dict(profile, venv="rocm72-b"), inst)
    assert r2["rev"] != r["rev"] and r2["argv"][1:] != r["argv"][1:]
    assert rd.build(profile, inst) == r                                  # deterministic
    assert rd.badge(r, r["argv"][1:]) == "match"
    assert rd.badge(r, r2["argv"][1:]) == "differs"
    assert rd.badge(r, None) == "unknown" and rd.badge(None, []) == "unassigned"
    win = rd.build(profile, dict(inst, platform="windows", paths={k: "C:\\farm\\" + k for k in inst["paths"]}))
    assert win["platform"] == "windows" and win["argv"][0] == "C:\\farm\\art\\venv\\rocm72-a\\Scripts\\python.exe"


def test_router_refuses_profiles_the_launcher_would_refuse(tmp_path):
    profile, inst = make_world(tmp_path)
    rd.check(profile, inst)
    with pytest.raises(rd.BadProfile, match="cannot be overridden"):
        rd.check(dict(profile, flags=["--output-directory", "/tmp"]), inst)
    with pytest.raises(rd.BadProfile, match="not allowed"):
        rd.check(dict(profile, flags=["--enable-manager"]), inst)
    with pytest.raises(rd.BadProfile, match="not allowed"):                # QA-6: not a published artifact (D-37)
        rd.check(dict(profile, flags=["--front-end-version", "Comfy-Org/ComfyUI_frontend@latest"]), inst)
    with pytest.raises(rd.BadProfile, match="environment"):
        rd.check(dict(profile, env={"LD_PRELOAD": "/tmp/x.so"}), inst)


def bad_cases(r, p):
    """(label, mutate(render)) — every one must be refused."""
    art, local = p["art"], p["local"]

    def argv_set(i, v):
        def f(x): x["argv"][i] = v
        return f

    def argv_add(*tokens):
        def f(x): x["argv"] += list(tokens)
        return f

    def yaml_path(x):
        return [a for a in x["argv"] if a.endswith(".yaml")][0]
    return [
        ("other contract major", lambda x: x.update(contract="comfy-upgrade/2")),
        ("other instance", lambda x: x.update(instance="gpu-2")),
        ("windows render on posix", lambda x: x.update(platform="windows")),
        ("system python", argv_set(0, "/usr/bin/python3")),
        ("venv escape", argv_set(0, f"{art}/venv/../../bin/python")),
        ("main.py elsewhere", argv_set(1, "/tmp/main.py")),
        ("unknown option", argv_add("--enable-manager")),
        ("base directory", argv_add("--base-directory", local)),
        ("database url", argv_add("--database-url", "sqlite:////tmp/x.db")),
        ("path outside prefixes", argv_add("--disabled-nodes-config", "/etc/passwd")),
        ("relative path", argv_add("--disabled-nodes-config", "x.json")),
        ("output inside the artifact store", lambda x: x["argv"].__setitem__(x["argv"].index("--output-directory") + 1, f"{art}/core")),
        ("second yaml from the input dir", argv_add("--extra-model-paths-config", f"{p['input']}/evil.yaml")),
        ("yaml loads nodes from the input dir", lambda x: x["files"].__setitem__(yaml_path(x), launcher.nodes_yaml([p["input"]]))),
        ("yaml outside the run dir", lambda x: x.update(files={f"{local}/evil.yaml": "comfyfarm: {}\n"})),
        ("frontend from anywhere", argv_add("--front-end-root", "/tmp/fe")),
        ("frontend downloaded at runtime", argv_add("--front-end-version", "Comfy-Org/ComfyUI_frontend@latest")),
        ("LD_PRELOAD", lambda x: x["env"].update(LD_PRELOAD="/tmp/x.so")),
        ("PYTHONPATH", lambda x: x["env"].update(PYTHONPATH="/tmp")),
        ("sync from outside the store", lambda x: x["sync"][0].update(src="/tmp/core")),
        ("sync over another place", lambda x: x["sync"][1].update(dst=f"{local}/core/x")),
        ("venv synced", lambda x: x["sync"].append({"kind": "venv", "name": "v", "src": f"{art}/venv/v", "dst": f"{local}/venv/v"})),
        ("core not the synced one", lambda x: x["sync"][0].update(name="other", src=f"{art}/core/other", dst=f"{local}/core/other")),
        ("cwd elsewhere", lambda x: x.update(cwd="/tmp")),
        ("mkdir in the store", lambda x: x["mkdirs"].append(f"{art}/x")),
        ("mkdir anywhere", lambda x: x["mkdirs"].append("/etc/comfy")),
        ("require dropped", lambda x: x.update(require=x["require"][1:])),
        ("require odd path", lambda x: x["require"].append("/tmp/READY")),
        ("bad rev", lambda x: x.update(rev="xyz")),
        ("option without value", argv_add("--port")),
    ]


def test_launcher_refuses_every_bad_render(tmp_path):
    profile, inst = make_world(tmp_path)
    good = rd.build(profile, inst)
    validate(good, inst)                                                  # positive control
    for label, mutate in bad_cases(good, inst["paths"]):
        r = copy.deepcopy(good)
        mutate(r)
        with pytest.raises(launcher.Refused):
            validate(r, inst)
            pytest.fail(f"accepted: {label}")
    # an artifact that is not READY: only visible on the worker
    os.remove(os.path.join(inst["paths"]["art"], "venv", "rocm72-a", "READY"))
    validate(good, inst, check_files=False)
    with pytest.raises(launcher.Refused, match="not ready"):
        validate(good, inst)


# ---- the launcher process ----------------------------------------------------------------------------------------
class FakeRouter:
    def __init__(self):
        self.status, self.body, self.calls = 200, b"{}", 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.calls += 1
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *a):
                pass
        self.port = free_port()
        self.srv = HTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def test_launcher_run(tmp_path, monkeypatch, capsys):
    profile, inst = make_world(tmp_path)
    good = rd.build(profile, inst)
    fr = FakeRouter()
    fr.body = json.dumps(good).encode()
    p = inst["paths"]
    for k, v in {"ROUTER": f"http://127.0.0.1:{fr.port}", "INSTANCE": "gpu-1", "ARTIFACTS": p["art"], "LOCAL": p["local"],
                 "PREFIXES": ",".join(p[k] for k in ("models", "input", "output", "user", "temp"))}.items():
        monkeypatch.setenv(f"COMFY_LAUNCH_{k}", v)
    monkeypatch.setenv("LD_PRELOAD", "/should/not/matter")
    execs = []
    monkeypatch.setattr(os, "execve", lambda path, argv, env: execs.append((path, argv, env)))
    monkeypatch.chdir(tmp_path)
    try:
        assert launcher.main(["--check"]) == 0 and not execs
        launcher.main([])
        path, argv, env = execs[-1]
        assert argv == good["argv"] and env["PYTORCH_TUNABLEOP_ENABLED"] == "1"
        assert not any(k.startswith("COMFY_LAUNCH_") for k in env)
        core_copy = os.path.join(p["local"], "core", profile["core"], "main.py")
        assert open(core_copy).read() == "print('comfy')\n"                       # synced
        yaml = good["argv"][good["argv"].index("--extra-model-paths-config") + 1]
        assert open(yaml).read() == launcher.nodes_yaml([os.path.join(p["local"], "nodes", "base-nodes")])
        assert all(os.path.isdir(d) for d in good["mkdirs"])

        # router down → the saved render (same rev) still starts the worker
        fr.status, fr.body = 503, b"down"
        launcher.main([])
        assert execs[-1][1] == good["argv"]
        # router says no → refused, the saved render is NOT used
        for code in (403, 404):
            fr.status = code
            assert launcher.main([]) == 2
        assert "refused" in capsys.readouterr().err
        # a bad render from the router → refused before anything is written
        fr.status, bad = 200, copy.deepcopy(good)
        bad["argv"] += ["--enable-manager"]
        fr.body = json.dumps(bad).encode()
        n = len(execs)
        assert launcher.main([]) == 2 and len(execs) == n
    finally:
        fr.close()


def test_sync_skips_current_copies_and_publish_hash(tmp_path):
    src, dst = tmp_path / "art/core/c1", tmp_path / "local/core/c1"
    src.mkdir(parents=True)
    (src / "a.py").write_text("x")
    (src / "sub").mkdir()
    (src / "sub/b.py").write_text("yy")
    h = launcher.tree_sha256(str(src))
    (src / "manifest.json").write_text(json.dumps({"tree_sha256": h}))
    (src / "READY").write_text("")
    assert launcher.tree_sha256(str(src)) == h                    # manifest and READY do not change the hash
    launcher.sync({"src": str(src), "dst": str(dst)})
    assert (dst / "sub/b.py").read_text() == "yy"
    (dst / "marker").write_text("local")                           # a current copy is left alone…
    launcher.sync({"src": str(src), "dst": str(dst)})
    assert (dst / "marker").exists()
    (src / "a.py").write_text("changed!")                          # …a different one is replaced whole
    (src / "manifest.json").write_text(json.dumps({"tree_sha256": launcher.tree_sha256(str(src))}))
    launcher.sync({"src": str(src), "dst": str(dst)})
    assert not (dst / "marker").exists() and (dst / "a.py").read_text() == "changed!"


def test_user_writable_dirs_stay_out_of_code(tmp_path):
    """QA-1: uploads must never land where ComfyUI imports code — not inside <LOCAL> (core and node copies) or <ART>,
    and not in a parent of them either (an upload's subfolder reaches everything below the input directory)."""
    from fastapi import HTTPException
    from comfyfarm.api.rollout import InstanceIn, check_instance
    profile, inst = make_world(tmp_path)
    p = inst["paths"]
    every = [p[k] for k in ("models", "input", "output", "user", "temp")] + [str(tmp_path)]
    validate(rd.build(profile, inst), inst, prefixes=every)                         # positive control
    check_instance(InstanceIn(**{**inst, "port": 8188}))
    for key, value in [("input", f"{p['local']}/core/{profile['core']}/custom_nodes"), ("user", f"{p['local']}/nodes/base-nodes"),
                       ("input", p["local"]), ("temp", f"{p['local']}/run/gpu-1/tmp"), ("output", str(tmp_path))]:
        bad = dict(inst, paths={**p, key: value})
        with pytest.raises(launcher.Refused, match="outside <ART> and <LOCAL>"):
            validate(rd.build(profile, bad), bad, prefixes=every)
        with pytest.raises(rd.BadProfile, match="outside <ART> and <LOCAL>"):
            rd.check(profile, bad)
        with pytest.raises(HTTPException, match="overlaps"):
            check_instance(InstanceIn(**{**bad, "port": 8188}))
    win = dict(inst, platform="windows", paths={k: "C:\\farm\\" + k for k in p})
    check_instance(InstanceIn(**{**win, "port": 8188}))
    with pytest.raises(HTTPException, match="overlaps"):
        check_instance(InstanceIn(**{**win, "paths": {**win["paths"], "input": "c:\\FARM\\local\\nodes\\x"}, "port": 8188}))


def test_env_values_that_name_paths_are_refused(tmp_path):
    """QA-2: the prefixes (TORCH_, TRITON_, HSA_, ...) also cover variables that load plugin libraries, tool binaries or
    compiled kernels. Values that are paths are refused; what real profiles need still passes."""
    from fastapi import HTTPException
    from comfyfarm.api.rollout import check_profile
    profile, inst = make_world(tmp_path)
    needed = {"HSA_OVERRIDE_GFX_VERSION": "12.0.1", "HSA_TOOLS_DISABLE_REGISTER": "1", "HIP_VISIBLE_DEVICES": "0",
              "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True,garbage_collection_threshold:0.6",
              "CUDA_VISIBLE_DEVICES": "0,1", "MIOPEN_FIND_MODE": "FAST", "TORCH_CUDA_ARCH_LIST": "8.6;12.0+PTX",
              "COMFYFARM_ALLOW": "10.0.0.1,10.0.0.2,10.0.1.0/24", "TZ": "Asia/Seoul", "OMP_NUM_THREADS": "8"}
    good = dict(profile, env=needed)
    validate(rd.build(good, inst), inst)                                     # positive control
    rd.check(good, inst)
    p = inst["paths"]
    for k, v in [("HSA_TOOLS_LIB", f"{p['input']}/evil.so"), ("TRITON_PTXAS_PATH", "/tmp/ptxas"),
                 ("MIOPEN_CUSTOM_CACHE_DIR", p["input"]), ("TRITON_CACHE_DIR", "../../run"), ("TORCH_EXTENSIONS_DIR", "~/ext"),
                 ("NCCL_NET_PLUGIN", "x:./libevil.so"), ("ROCM_PATH", "C:\\rocm"), ("TZ", "/data/run/in/zone"),
                 ("COMFYFARM_ALLOW", "10.0.0.1,/etc/x"), ("HIP_VISIBLE_DEVICES", "0\n1")]:
        r = rd.build(dict(profile, env={k: v}), inst)
        with pytest.raises(launcher.Refused, match=k):
            validate(r, inst)
        with pytest.raises(rd.BadProfile):
            rd.check(dict(profile, env={k: v}), inst)
        assert launcher.env_problem(k, v) and "\n" not in launcher.env_problem(k, v)
        with pytest.raises(HTTPException, match="environment"):
            check_profile(dict(profile, env={k: v}))
