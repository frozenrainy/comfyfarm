"""Host check (docs/design/14): a fake host under tmp_path (devices, sysfs, linger) and a fake command runner."""
import http.server
import json
import os
import socket
import threading

import pytest

from comfyfarm import hostcheck


@pytest.fixture
def router():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def host(tmp_path, *, kfd=True, kfd_mode=0o666, linger=True, gfx=120001):
    root = tmp_path / "root"
    (root / "dev" / "dri").mkdir(parents=True)
    if kfd:
        (root / "dev" / "kfd").write_text(""); os.chmod(root / "dev" / "kfd", kfd_mode)
        (root / "dev" / "dri" / "renderD128").write_text(""); os.chmod(root / "dev" / "dri" / "renderD128", kfd_mode)
        node = root / "sys" / "class" / "kfd" / "kfd" / "topology" / "nodes"
        (node / "0").mkdir(parents=True); (node / "0" / "properties").write_text("cpu_cores_count 12\ngfx_target_version 0\n")
        (node / "1").mkdir(parents=True); (node / "1" / "properties").write_text(f"gfx_target_version {gfx}\n")
        (root / "sys" / "module" / "amdgpu").mkdir(parents=True); (root / "sys" / "module" / "amdgpu" / "version").write_text("6.16.6\n")
    if linger:
        (root / "var" / "lib" / "systemd" / "linger").mkdir(parents=True)
        (root / "var" / "lib" / "systemd" / "linger" / "comfyfarm").write_text("")
    farm = tmp_path / "farm"
    paths = {k: str(farm / k) for k in ("input", "output", "user", "temp", "models")}
    for p in paths.values():
        os.makedirs(p)
    art, local = farm / "art", tmp_path / "local"
    (art / "core" / "c1").mkdir(parents=True); (art / "core" / "c1" / "READY").write_text("")
    local.mkdir()
    return str(root), paths, str(art), str(local)


def fake_cmd(answers=None):
    answers = answers or {}

    def run(argv, timeout=20):
        key = " ".join(argv[:3])
        for k, v in answers.items():
            if key.startswith(k):
                return v
        if argv[:2] == ["systemctl", "--user"]:
            return 0, "running"
        if argv[0] == "timedatectl":
            return 0, "yes"
        return 127, "not found"
    return run


def levels(out):
    return {r["name"]: r["level"] for r in out}


def check(tmp_path, router, *, cfg_extra=None, cmd=None, **host_kw):
    root, paths, art, local = host(tmp_path, **host_kw)
    env = {"COMFY_LAUNCH_ROUTER": router, "COMFY_LAUNCH_ARTIFACTS": art, "COMFY_LAUNCH_LOCAL": local}
    cfg = {"gpu": "amd", "paths": paths, "port": free_port(), "listen": "127.0.0.1", "instance": "gpu-1", **(cfg_extra or {})}
    return hostcheck.Check(env, cfg, root=root, cmd=cmd or fake_cmd(), user="comfyfarm").run(), paths


def test_a_ready_amd_host(tmp_path, router):
    out, paths = check(tmp_path, router)
    lv = levels(out)
    assert lv["GPU (AMD)"] == "OK" and "gfx1201" in next(r["detail"] for r in out if r["name"] == "GPU (AMD)")
    assert all(lv[k] == "OK" for k in ("lingering", "user manager", "artifact store", "local root", "input directory",
                                         "router", "port", "clock")), out
    assert not [r for r in out if r["level"] == "FAIL"], out
    assert os.listdir(os.path.join(paths["output"], ".comfyfarm-hosts"))          # the shared-storage marker


def test_what_a_fresh_host_gets_wrong(tmp_path, router):
    out, _ = check(tmp_path, router, kfd_mode=0o000, linger=False,
                   cmd=fake_cmd({"timedatectl": (0, "no"), "systemctl --user is-system-running": (1, "offline")}))
    lv = levels(out)
    if os.getuid() != 0:                                     # root can open anything; the test runs as a user
        assert lv["GPU (AMD)"] == "FAIL" and lv["GPU"] == "FAIL"
    assert lv["lingering"] == "FAIL" and lv["user manager"] == "FAIL" and lv["clock"] == "WARN"


def test_no_gpu_where_one_is_expected_and_where_none_is(tmp_path, router):
    out, _ = check(tmp_path, router, kfd=False)
    assert levels(out)["GPU"] == "FAIL"
    out, _ = check(tmp_path / "b", router, kfd=False, cfg_extra={"gpu": "any"})
    assert levels(out)["GPU"] == "WARN"


def test_paths_that_the_launcher_would_refuse(tmp_path, router):
    root, paths, art, local = host(tmp_path)
    env = {"COMFY_LAUNCH_ROUTER": router, "COMFY_LAUNCH_ARTIFACTS": art, "COMFY_LAUNCH_LOCAL": local}
    os.makedirs(f"{local}/nodes/n1")
    bad = dict(paths, input=f"{local}/nodes/n1")
    out = hostcheck.Check(env, {"gpu": "amd", "paths": bad}, root=root, cmd=fake_cmd(), user="comfyfarm").run()
    assert any(r["name"] == "input directory" and r["level"] == "FAIL" and "overlaps" in r["detail"] for r in out)


def test_a_venv_from_another_host_does_not_run(tmp_path, router):
    root, paths, art, local = host(tmp_path)
    v = os.path.join(art, "venv", "built-elsewhere")
    os.makedirs(os.path.join(v, "bin")); open(os.path.join(v, "READY"), "w").close()
    os.symlink("/home/builder/.local/share/uv/python/cpython-3.12/bin/python3.12", os.path.join(v, "bin", "python"))
    env = {"COMFY_LAUNCH_ROUTER": router, "COMFY_LAUNCH_ARTIFACTS": art, "COMFY_LAUNCH_LOCAL": local}
    out = hostcheck.Check(env, {"gpu": "amd", "paths": paths}, root=root, user="comfyfarm").run()   # real commands
    assert levels(out)["venv built-elsewhere"] == "FAIL"


def test_router_down_and_port_taken(tmp_path):
    busy = socket.socket(); busy.bind(("127.0.0.1", 0)); busy.listen()
    try:
        out, _ = check(tmp_path, "http://127.0.0.1:9", cfg_extra={"port": busy.getsockname()[1]},
                       cmd=fake_cmd({"systemctl --user is-active": (3, "inactive")}))
    finally:
        busy.close()
    lv = levels(out)
    assert lv["router"] == "FAIL" and lv["port"] == "FAIL"


def test_cli_reads_the_instance_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hostcheck, "ETC", str(tmp_path))
    assert hostcheck.main(["gpu-1"]) == 2                                            # not set up
    (tmp_path / "gpu-1.env").write_text("COMFY_LAUNCH_ROUTER=http://127.0.0.1:9\n")
    (tmp_path / "gpu-1.check.json").write_text(json.dumps({"gpu": "none", "paths": {}}))
    assert hostcheck.main(["gpu-1", "--json"]) == 1                                  # router down, no store
    assert any(r["name"] == "router" for r in json.loads(capsys.readouterr().out))


def test_a_venv_missing_a_system_library_names_the_package(tmp_path, router):
    root, paths, art, local = host(tmp_path)
    v = os.path.join(art, "venv", "rocm-x")
    os.makedirs(os.path.join(v, "bin")); open(os.path.join(v, "READY"), "w").close()
    env = {"COMFY_LAUNCH_ROUTER": router, "COMFY_LAUNCH_ARTIFACTS": art, "COMFY_LAUNCH_LOCAL": local}
    err = (1, "Traceback …\nImportError: libatomic.so.1: cannot open shared object file: No such file or directory")
    out = hostcheck.Check(env, {"gpu": "amd", "paths": paths}, root=root, cmd=fake_cmd({v: err}), user="comfyfarm").run()
    r = next(x for x in out if x["name"] == "venv rocm-x")
    assert r["level"] == "FAIL" and "libatomic.so.1" in r["detail"] and "libatomic1" in r["detail"]
    ok = fake_cmd({v: (0, "comfyfarm-venv: 3.12.15 · torch 2.14.1+rocm7.2\n(null): No such file or directory")})
    out = hostcheck.Check(env, {"gpu": "amd", "paths": paths}, root=root, cmd=ok, user="comfyfarm").run()
    r = next(x for x in out if x["name"] == "venv rocm-x")
    assert r["level"] == "OK" and "torch 2.14.1+rocm7.2" in r["detail"] and "(null)" not in r["detail"]


def test_only_the_venvs_this_host_can_use_are_checked(tmp_path, router):
    """Rehearsal: the UI worker's host (no GPU) failed on the ROCm venv it will never run."""
    root, paths, art, local = host(tmp_path, kfd=False)
    for name, accel in (("rocm-v", "rocm"), ("cuda-v", "cuda"), ("cpu-v", "cpu")):
        v = os.path.join(art, "venv", name)
        os.makedirs(os.path.join(v, "bin")); open(os.path.join(v, "READY"), "w").close()
        with open(os.path.join(v, "manifest.json"), "w") as f:
            json.dump({"accelerator": accel}, f)
    env = {"COMFY_LAUNCH_ROUTER": router, "COMFY_LAUNCH_ARTIFACTS": art, "COMFY_LAUNCH_LOCAL": local}
    names = lambda gpu: sorted(r["name"] for r in hostcheck.Check(env, {"gpu": gpu, "paths": paths}, root=root,   # noqa: E731
                                                                     cmd=fake_cmd(), user="comfyfarm").run() if r["name"].startswith("venv"))
    assert names("none") == ["venv cpu-v"]
    assert names("amd") == ["venv cpu-v", "venv rocm-v"]
    assert names("nvidia") == ["venv cpu-v", "venv cuda-v"]
    assert names("any") == ["venv cpu-v", "venv cuda-v", "venv rocm-v"]


def test_recipe_templates_in_the_store_are_reported_with_the_way_to_make_them(tmp_path, router):
    out, _ = check(tmp_path / "a", router)
    line = next(r for r in out if r["name"] == "recipes")
    assert line["level"] == "WARN" and "publish.py recipe" in line["detail"] and "rocm-gfx1201" in line["detail"]
    root, paths, art, local = host(tmp_path / "b")
    os.makedirs(os.path.join(art, "profiles"))
    t = {"template": "rocm-gfx1201-1", "recipe": "rocm-gfx1201",
         "gpu": {"core": "c1", "venv": "rocm72-1", "nodes": ["farm-gpu-1"], "flags": [], "env": {}},
         "ui": {"core": "c1", "venv": "cpu-1", "nodes": ["farm-ui-1"], "flags": ["--cpu"], "env": {}}}
    for kind, name in (("venv", "rocm72-1"), ("venv", "cpu-1"), ("nodes", "farm-gpu-1"), ("nodes", "farm-ui-1")):
        os.makedirs(os.path.join(art, kind, name))
        open(os.path.join(art, kind, name, "READY"), "w").close()
    with open(os.path.join(art, "profiles", "rocm-gfx1201-1.json"), "w") as f:
        json.dump(t, f)
    with open(os.path.join(art, "profiles", "rocm-gfx1201-2.json"), "w") as f:          # points at a retired venv
        json.dump({**t, "template": "rocm-gfx1201-2", "gpu": {**t["gpu"], "venv": "rocm72-9"}}, f)
    with open(os.path.join(art, "profiles", "broken.json"), "w") as f:
        f.write("{not json")
    env = {"COMFY_LAUNCH_ROUTER": router, "COMFY_LAUNCH_ARTIFACTS": art, "COMFY_LAUNCH_LOCAL": local}
    cfg = {"gpu": "amd", "paths": paths, "port": free_port(), "listen": "127.0.0.1", "instance": "gpu-1"}
    out = hostcheck.Check(env, cfg, root=root, cmd=fake_cmd(), user="comfyfarm").run()
    line = next(r for r in out if r["name"] == "recipes")
    assert line["level"] == "OK" and line["templates"][0] == {**t, "missing": []}     # the broken file is skipped, not fatal
    assert line["templates"][1]["missing"] == ["venv/rocm72-9"] and "rocm-gfx1201-2" not in line["detail"]
    assert levels(out)["artifact store"] == "OK"                             # templates are not counted as artifacts


def test_a_gpu_venv_must_reach_the_gpu_not_only_import_torch(tmp_path, router):
    """A CUDA wheel newer than the driver imports fine and then finds no GPU: every job would run on nothing."""
    root, paths, art, local = host(tmp_path)
    v = os.path.join(art, "venv", "rocm-x")
    os.makedirs(os.path.join(v, "bin")); open(os.path.join(v, "READY"), "w").close()
    with open(os.path.join(v, "manifest.json"), "w") as f:
        json.dump({"accelerator": "rocm"}, f)
    env = {"COMFY_LAUNCH_ROUTER": router, "COMFY_LAUNCH_ARTIFACTS": art, "COMFY_LAUNCH_LOCAL": local}
    seen = []

    def run(answer):
        def cmd(argv, timeout=20):
            if argv[0].startswith(v):
                seen.append(argv[3:])
                return answer
            return fake_cmd()(argv, timeout)
        out = hostcheck.Check(env, {"gpu": "amd", "paths": paths}, root=root, cmd=cmd, user="comfyfarm").run()
        return next(x for x in out if x["name"] == "venv rocm-x")
    r = run((0, "comfyfarm-venv: 3.12.15 · torch 2.14.1+rocm7.2 · 1 GPU(s): AMD Radeon AI PRO R9700"))
    assert r["level"] == "OK" and "R9700" in r["detail"] and seen[-1] == ["gpu"]
    r = run((0, "comfyfarm-gpu-error: No HIP GPUs are available (torch built for HIP 7.2)\ncomfyfarm-venv: 3.12.15 · torch 2.14.1"))
    assert r["level"] == "FAIL" and "No HIP GPUs" in r["detail"] and "NVIDIA" not in r["detail"]
    # no GPU of that kind here (a UI host checking with gpu any): only the import is checked
    seen.clear()
    out = hostcheck.Check(env, {"gpu": "any", "paths": paths}, root=host(tmp_path / "b", kfd=False)[0],
                          cmd=lambda a, timeout=20: (seen.append(a[3:]), (0, "comfyfarm-venv: 3.12.15"))[1]
                          if a[0].startswith(v) else fake_cmd()(a, timeout), user="comfyfarm").run()
    assert seen == [[]]


def test_the_gpu_probe_reports_an_old_driver_for_real(tmp_path):
    """The probe itself, run by a Python without a GPU runtime: the probe's own syntax and the error path."""
    import subprocess, sys
    fake = tmp_path / "torch"
    fake.mkdir()
    (fake / "__init__.py").write_text(
        "__version__ = '2.14.1+cu130'\n"
        "class version: cuda = '13.0'; hip = None\n"
        "class cuda:\n"
        " @staticmethod\n"
        " def init(): raise RuntimeError('The NVIDIA driver on your system is too old (found version 12020).\\nmore')\n")
    captured = {}

    def cmd(argv, timeout=20):
        if argv[1:2] == ["-c"]:
            captured["probe"] = argv[2]
        return fake_cmd()(argv, timeout)
    root, paths, art, local = host(tmp_path / "h")
    v = os.path.join(art, "venv", "cu-x"); os.makedirs(os.path.join(v, "bin")); open(os.path.join(v, "READY"), "w").close()
    env = {"COMFY_LAUNCH_ROUTER": "http://127.0.0.1:9", "COMFY_LAUNCH_ARTIFACTS": art, "COMFY_LAUNCH_LOCAL": local}
    hostcheck.Check(env, {"gpu": "any", "paths": paths}, root=root, cmd=cmd, user="comfyfarm").run()
    p = subprocess.run([sys.executable, "-c", captured["probe"], "gpu"], capture_output=True, text=True,
                       env={"PYTHONPATH": str(tmp_path)})
    assert p.returncode == 0, p.stderr
    assert "comfyfarm-gpu-error: The NVIDIA driver on your system is too old (found version 12020). (torch built for CUDA 13.0)" in p.stdout
    p = subprocess.run([sys.executable, "-c", captured["probe"]], capture_output=True, text=True, env={"PYTHONPATH": str(tmp_path)})
    assert "gpu-error" not in p.stdout and "torch 2.14.1+cu130" in p.stdout


def test_a_uid_without_a_passwd_entry_still_checks(monkeypatch):
    # the UI worker container runs as the workers' uid, which its image's /etc/passwd does not list: the check printed
    # nothing (a KeyError before any output) and the dashboard showed an empty result
    def missing(uid):
        raise KeyError(f"getpwuid(): uid not found: {uid}")
    monkeypatch.setattr(hostcheck.pwd, "getpwuid", missing)
    monkeypatch.setenv("USER", "comfyfarm")
    assert hostcheck.Check({}, {"container": True}).user == "comfyfarm"
    monkeypatch.delenv("USER")
    assert hostcheck.Check({}, {"container": True}).user == f"uid {os.getuid()}"


def test_models_missing_empty_or_there(tmp_path, router):
    out, paths = check(tmp_path / "a", router)
    models = lambda o: next(r for r in o if r["name"] == "models")            # noqa: E731
    assert models(out)["level"] == "WARN" and "empty" in models(out)["detail"]
    # ComfyUI's own tree holds empty placeholders only: still nothing to load
    os.makedirs(os.path.join(paths["models"], "checkpoints"))
    open(os.path.join(paths["models"], "checkpoints", "put_checkpoints_here"), "w").close()
    root, _, art, local = host(tmp_path / "b")
    env = {"COMFY_LAUNCH_ROUTER": router, "COMFY_LAUNCH_ARTIFACTS": art, "COMFY_LAUNCH_LOCAL": local}
    run = lambda cfg: hostcheck.Check(env, cfg, root=root, cmd=fake_cmd(), user="comfyfarm").run()     # noqa: E731
    assert models(run({"gpu": "amd", "paths": paths}))["level"] == "WARN"
    with open(os.path.join(paths["models"], "checkpoints", "sd15.safetensors"), "wb") as f:
        f.write(b"x")
    assert models(run({"gpu": "amd", "paths": paths}))["level"] == "OK"
    # the UI worker needs none
    empty = dict(paths, models=str(tmp_path / "empty-models")); os.makedirs(empty["models"])
    assert models(run({"gpu": "none", "role": "ui", "paths": empty}))["level"] == "OK"
    gone = dict(paths, models=str(tmp_path / "no-models"))
    m = models(run({"gpu": "amd", "paths": gone}))
    assert m["level"] == "FAIL" and "mount the models there" in m["detail"]


def test_in_the_ui_worker_container_the_clock_is_the_hosts(tmp_path, router):
    """QA (dev.11): every install showed "clock not synchronised" for the UI worker — a container has no timedatectl."""
    asked = []

    def cmd(argv, timeout=20):
        asked.append(argv[0])
        return 127, "not found"
    out, _ = check(tmp_path, router, cfg_extra={"container": True, "gpu": "none", "role": "ui"}, cmd=cmd)
    clock = next(r for r in out if r["name"] == "clock")
    assert clock["level"] == "OK" and "timedatectl" not in asked
    out, _ = check(tmp_path / "b", router, cmd=cmd)                    # on a host it is still checked
    assert next(r for r in out if r["name"] == "clock")["level"] == "WARN"


def test_every_line_says_how_it_was_made(tmp_path, router):
    """Each line carries its English template and values, so the dashboard shows it in the reader's language; the
    template filled in must be the line itself (a template that drifted from the text would translate something else)."""
    out, _ = check(tmp_path / "a", router, kfd_mode=0o000, linger=False,
                   cmd=fake_cmd({"timedatectl": (0, "no"), "systemctl --user is-system-running": (1, "offline")}))
    out += check(tmp_path / "b", router)[0]
    templated = [r for r in out if "t" in r]
    assert len(templated) >= 10
    for r in templated:
        assert r["t"].format(**r["a"]) == r["detail"], r
        assert all(isinstance(v, str) for v in r["a"].values())
    plain = [r for r in out if "t" not in r]
    assert all(r["name"] in ("input directory", "output directory", "user directory", "temp directory", "models",
                             "GPU (NVIDIA)") for r in plain), plain        # only a path or a device list as it is


def test_the_port_is_where_the_instance_runs_now(tmp_path, router):
    """QA (dev.11): after Configure moved ComfyUI to one address and it restarted, the check still named the address
    setup was asked for (0.0.0.0). The launcher's saved render says where it runs."""
    root, paths, art, local = host(tmp_path)
    env = {"COMFY_LAUNCH_ROUTER": router, "COMFY_LAUNCH_ARTIFACTS": art, "COMFY_LAUNCH_LOCAL": local}
    cfg = {"gpu": "amd", "paths": paths, "port": 8188, "listen": "0.0.0.0", "instance": "gpu-1"}
    port = free_port()
    os.makedirs(f"{local}/run/gpu-1")
    with open(f"{local}/run/gpu-1/render.json", "w") as f:
        json.dump({"argv": ["python", "main.py", "--listen", "127.0.0.1", "--port", str(port), "--multi-user"]}, f)
    out = hostcheck.Check(env, cfg, root=root, cmd=fake_cmd(), user="comfyfarm").run()
    assert next(r for r in out if r["name"] == "port")["detail"] == f"127.0.0.1:{port} is free"
    os.remove(f"{local}/run/gpu-1/render.json")                    # never started: what setup was asked for
    out = hostcheck.Check(env, cfg, root=root, cmd=fake_cmd(), user="comfyfarm").run()
    assert next(r for r in out if r["name"] == "port")["detail"].startswith("0.0.0.0:8188 ")
