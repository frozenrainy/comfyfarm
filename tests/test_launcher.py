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
        # QA-8: malformed, not merely wrong — must be a refusal (exit 2), never a crash (exit 1: restart loop)
        ("sync entry not an object", lambda x: x["sync"].append("x")),
        ("sync as an object", lambda x: x.update(sync={"core": x["sync"][0]})),
        ("sync name not a string", lambda x: x["sync"][1].update(name=5)),
        ("files as a list", lambda x: x.update(files=list(x["files"]))),
        ("require as a string", lambda x: x.update(require=x["require"][0])),
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
        assert env["PIP_NO_INDEX"] == "1"                                            # no installs at run time
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


def test_launcher_malformed_render_exits_2(tmp_path, monkeypatch, capsys):
    """QA-8: a malformed render from the router is a refusal (exit 2, one line), not an exception (exit 1)."""
    profile, inst = make_world(tmp_path)
    fr = FakeRouter()
    p = inst["paths"]
    for k, v in {"ROUTER": f"http://127.0.0.1:{fr.port}", "INSTANCE": "gpu-1", "ARTIFACTS": p["art"], "LOCAL": p["local"],
                 "PREFIXES": ",".join(p[k] for k in ("models", "input", "output", "user", "temp"))}.items():
        monkeypatch.setenv(f"COMFY_LAUNCH_{k}", v)
    monkeypatch.setattr(os, "execve", lambda *a: pytest.fail("must not start"))
    try:
        for mutate in (lambda x: x["sync"].append("x"), lambda x: x.update(files=list(x["files"])), lambda x: x.update(env=[])):
            bad = rd.build(profile, inst)
            mutate(bad)
            fr.body = json.dumps(bad).encode()
            assert launcher.main(["--check"]) == 2
            err = capsys.readouterr().err
            assert "refused" in err and err.count("\n") == 1
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
    """QA-1: uploads must never land where ComfyUI imports code — not inside <ART> or the launcher-managed parts of
    <LOCAL> (core, nodes, frontend, run), and not in a parent of them either (an upload's subfolder reaches everything
    below the input directory). Elsewhere under <LOCAL>, a temp directory say, is fine (contract §4)."""
    from fastapi import HTTPException
    from comfyfarm.api.rollout import InstanceIn, check_instance
    profile, inst = make_world(tmp_path)
    p = inst["paths"]
    every = [p[k] for k in ("models", "input", "output", "user", "temp")] + [str(tmp_path)]
    validate(rd.build(profile, inst), inst, prefixes=every)                         # positive control
    check_instance(InstanceIn(**{**inst, "port": 8188}))
    local_temp = dict(inst, paths={**p, "temp": f"{p['local']}/temp/gpu-1"})          # <LOCAL>/temp: not managed
    validate(rd.build(profile, local_temp), local_temp, prefixes=every)
    rd.check(profile, local_temp)
    check_instance(InstanceIn(**{**local_temp, "port": 8188}))
    for key, value in [("input", f"{p['local']}/core/{profile['core']}/custom_nodes"), ("user", f"{p['local']}/nodes/base-nodes"),
                       ("input", p["local"]), ("temp", f"{p['local']}/run/gpu-1/tmp"), ("output", str(tmp_path))]:
        bad = dict(inst, paths={**p, key: value})
        with pytest.raises(launcher.Refused, match="must not overlap <ART> or <LOCAL>"):
            validate(rd.build(profile, bad), bad, prefixes=every)
        with pytest.raises(rd.BadProfile, match="must not overlap <ART> or <LOCAL>"):
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
                 ("COMFYFARM_ALLOW", "10.0.0.1,/etc/x"), ("HIP_VISIBLE_DEVICES", "0\n1"), ("OMP_NUM_THREADS", "$(id)"),
                 ("TZ", "UTC+9;rm"), ("MIOPEN_FIND_MODE", "%TEMP%")]:
        r = rd.build(dict(profile, env={k: v}), inst)
        with pytest.raises(launcher.Refused, match=k):
            validate(r, inst)
        with pytest.raises(rd.BadProfile):
            rd.check(dict(profile, env={k: v}), inst)
        assert launcher.env_problem(k, v) and "\n" not in launcher.env_problem(k, v)
        with pytest.raises(HTTPException, match="environment"):
            check_profile(dict(profile, env={k: v}))


def test_prune_keeps_what_any_instance_runs_and_orders_by_use(tmp_path):
    """QA-5: with a shared <LOCAL>, one launcher must not delete what another instance runs; "most recently used" is
    the launcher's own stamp, not the publish time that copytree carries over."""
    import time
    art, local = tmp_path / "art", tmp_path / "local"

    def published(name, mtime):
        d = art / "core" / name
        d.mkdir(parents=True)
        (d / "main.py").write_text(name)
        (d / "manifest.json").write_text(json.dumps({"tree_sha256": "h-" + name}))
        os.utime(d, (mtime, mtime))
        return {"kind": "core", "name": name, "src": str(d), "dst": str(local / "core" / name)}
    entries = {n: published(n, 1000 + i * 100) for i, n in enumerate(["c1", "c2", "c3", "c4", "c5"])}
    for n in ["c5", "c4", "c3", "c2", "c1"]:                              # used newest-published first
        launcher.sync(entries[n])
        time.sleep(0.02)
    launcher.prune(str(local), "core", set())
    assert sorted(os.listdir(local / "core")) == ["c1", "c2", "c3"]       # the three used last, whatever their age

    # another instance (gpu-2) on the same <LOCAL> runs c5: kept although it is the least recently used
    launcher.sync(entries["c5"])
    run2 = local / "run" / "gpu-2"
    run2.mkdir(parents=True)
    (run2 / "render.json").write_text(json.dumps({"sync": [entries["c5"]]}))
    time.sleep(0.02)
    for n in ["c4", "c3", "c2", "c1"]:
        launcher.sync(entries[n])
        time.sleep(0.02)
    (local / "core" / "c9.tmp-4242").mkdir()                               # another launcher mid-copy
    launcher.prune(str(local), "core", {"c1"})
    assert sorted(os.listdir(local / "core")) == ["c1", "c2", "c3", "c5", "c9.tmp-4242"]
    # an unreadable run state: nothing is removed (what it references is unknown)
    (run2 / "render.json").write_text("{not json")
    launcher.sync(entries["c4"])
    launcher.prune(str(local), "core", {"c4"})
    assert "c1" in os.listdir(local / "core") and "c5" in os.listdir(local / "core")


def test_env_token_edges():
    """Contract §4 token: empty is valid (a UI worker with every GPU hidden), letters are ASCII only, and every part —
    not only the first character — is checked for a leading '.'."""
    assert launcher.env_problem("CUDA_VISIBLE_DEVICES", "") is None
    assert launcher.env_problem("MIOPEN_FIND_MODE", "빠름")
    assert launcher.env_problem("MIOPEN_FIND_MODE", "LAZY,.x")



def test_comfyui_gets_the_venvs_nvidia_libraries_and_no_runtime_installs(tmp_path):
    """Field (CUDA, onnxruntime-gpu 1.31): without the venv's nvidia/*/lib on LD_LIBRARY_PATH its CUDA provider fails
    and every ONNX node runs on the CPU, silently — cuDNN's directory alone was not enough. And a node pack that
    pip-installs while ComfyUI imports it must fail at once, not wait for a network."""
    from comfyfarm import launcher
    venv = tmp_path / "art" / "venv" / "cu130-1"
    for lib in ("cudnn", "cublas", "cuda_runtime"):
        (venv / "lib" / "python3.12" / "site-packages" / "nvidia" / lib / "lib").mkdir(parents=True)
    env = launcher.runtime_env(str(venv / "bin" / "python"), {"LD_LIBRARY_PATH": "/opt/x"})
    paths = env["LD_LIBRARY_PATH"].split(":")
    assert [p.split("/nvidia/")[1] for p in paths[:3]] == ["cublas/lib", "cuda_runtime/lib", "cudnn/lib"] and paths[3] == "/opt/x"
    assert env["PIP_NO_INDEX"] == "1"
    cpu = launcher.runtime_env(str(tmp_path / "art" / "venv" / "cpu-1" / "bin" / "python"), {})
    assert "LD_LIBRARY_PATH" not in cpu and cpu["PIP_NO_INDEX"] == "1"


def test_copy_mode_waits_for_the_artifacts_then_starts(tmp_path, monkeypatch, capsys):
    """docs/design/16: with COMFY_LAUNCH_FETCH=1 the launcher asks this host's fetch service for what the render needs
    and is not here, waits, and goes on; a failed copy is a refusal (exit 2, no restart loop)."""
    import threading
    import time
    from comfyfarm import fetch
    store, art = tmp_path / "store", tmp_path / "art"
    (store / "requests").mkdir(parents=True); (store / "status").mkdir()
    monkeypatch.setattr(fetch, "REQUESTS", str(store / "requests"))
    monkeypatch.setattr(fetch, "STATUS", str(store / "status"))
    (art / "core" / "c-1").mkdir(parents=True); (art / "core" / "c-1" / "READY").write_text("")      # one is here
    render = {"require": [f"{art}/core/c-1/READY", f"{art}/venv/v-1/READY"]}

    def service():
        time.sleep(0.3)
        assert sorted(os.listdir(store / "requests")) == ["venv.v-1"]          # only what is missing was asked for
        os.remove(store / "requests" / "venv.v-1")
        (art / "venv" / "v-1").mkdir(parents=True); (art / "venv" / "v-1" / "READY").write_text("")
    threading.Thread(target=service).start()
    launcher.copies(render, str(art), timeout=10)
    assert "waiting for copies of venv/v-1" in capsys.readouterr().err
    launcher.copies(render, str(art), timeout=10)                               # all here: nothing asked
    assert os.listdir(store / "requests") == []

    render["require"].append(f"{art}/nodes/n-1/READY")
    (store / "status" / "nodes.n-1.json").write_text(json.dumps({"state": "failed", "error": "not signed by this farm's builder"}))

    def fails():
        time.sleep(0.3)
        os.remove(store / "requests" / "nodes.n-1")                             # taken, tried again, failed again
    threading.Thread(target=fails).start()
    with pytest.raises(launcher.Refused, match="copying nodes/n-1 failed: not signed"):
        launcher.copies(render, str(art), timeout=10)


def test_the_farm_model_store_is_the_hosts_own_setting(tmp_path):
    """docs/design/17: an instance with a model store gets it in its extra-model-paths file beside the custom nodes; the
    launcher accepts only the store its host was set up with — a render cannot point the models anywhere else."""
    profile, inst = make_world(tmp_path)
    store = str(tmp_path / "model-store")
    plain = rd.build(profile, inst)
    with_store = rd.build(profile, dict(inst, paths=dict(inst["paths"], model_store=store)))
    content = next(iter(with_store["files"].values()))
    assert f"comfyfarm_models:\n  base_path: {store}\n  checkpoints: checkpoints\n" in content
    assert "custom_nodes" in content and with_store["rev"] != plain["rev"]
    p = inst["paths"]
    prefixes = [p[k] for k in ("models", "input", "output", "user", "temp")]
    launcher.validate(with_store, instance="gpu-1", art=p["art"], local=p["local"], prefixes=prefixes, model_store=store)
    for host_store in (None, str(tmp_path / "elsewhere")):                    # the host set up otherwise
        with pytest.raises(launcher.Refused, match="not the synced node artifacts and this host's model store"):
            launcher.validate(with_store, instance="gpu-1", art=p["art"], local=p["local"], prefixes=prefixes,
                              model_store=host_store)
    validate(plain, inst)                                                     # an instance without one: as before
    # a store users can write into would let an upload be loaded as a model
    assert rd.path_problems(dict(p, model_store=p["input"]), False) == ["paths.input overlaps paths.model_store"]
    assert rd.path_problems(dict(p, model_store=store), False) == []
