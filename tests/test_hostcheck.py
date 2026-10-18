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
    ok = fake_cmd({v: (0, "3.12.15 · torch 2.14.1+rocm7.2")})
    out = hostcheck.Check(env, {"gpu": "amd", "paths": paths}, root=root, cmd=ok, user="comfyfarm").run()
    assert next(x for x in out if x["name"] == "venv rocm-x")["level"] == "OK"
