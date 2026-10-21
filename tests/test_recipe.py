"""publish.py recipe: one command publishes what a farm needs and a profile template; a second run reuses everything."""
import json
import os
import subprocess
import sys

import pytest

from comfyfarm import publish

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git_repo(path, files, tag=None):
    path.mkdir(parents=True)
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, env=env, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("add", "-A")
    run("commit", "-qm", "x")
    if tag:
        run("tag", tag)
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    comfy = tmp_path / "ComfyUI"
    git_repo(comfy, {"main.py": "print('comfy')\n", "requirements.txt": "torch\n"}, tag="v0.41.0")
    node = tmp_path / "SomeNode"
    commit = git_repo(node, {"__init__.py": "NODE_CLASS_MAPPINGS = {}\n", "web/x.js": "//\n"})
    recipe = {"recipe": "test-gpu", "comfyui": {"repo": str(comfy), "ref": "v0.41.0"}, "python": "3.12",
              "gpu": {"venv": "gv", "accelerator": "rocm", "arch": ["gfx1201"], "index": "https://example.invalid/rocm",
                      "env": {"HSA_TOOLS_DISABLE_REGISTER": "1"}, "flags": []},
              "ui": {"venv": "uv", "accelerator": "cpu", "index": "https://example.invalid/cpu", "env": {}, "flags": ["--cpu"]},
              "nodes": [{"name": "SomeNode", "repo": str(node), "commit": commit, "requirements": ["imageio-ffmpeg"], "ui": True}]}
    rdir = tmp_path / "rc" / "recipes"
    rdir.mkdir(parents=True)
    (rdir / "test-gpu.json").write_text(json.dumps(recipe))
    built = []

    def fake_venv(art, name, python, reqs, acc, arch, pip, uv=None, manifest_extra=None, constraints=None, overrides=None):
        built.append((name, [open(r).read() for r in reqs], pip,                      # a real one is GBs
                      [open(c).read() for c in constraints or []], [open(o).read() for o in overrides or []]))
        d = os.path.join(art, "venv", name)
        os.makedirs(d)
        m = publish.write_manifest(d, "venv", name, accelerator=acc, **(manifest_extra or {}))
        open(os.path.join(d, "READY"), "w").close()
        return m
    monkeypatch.setattr(publish, "publish_venv", fake_venv)
    monkeypatch.setattr(publish, "needed_through_dependencies", lambda *a, **kw: [])   # resolving needs the real indexes
    return {"art": tmp_path / "art", "recipes": str(tmp_path / "rc"), "built": built, "node": node, "recipe": recipe,
            "rdir": rdir, "tmp": tmp_path}


def run(s, **kw):
    return publish.publish_recipe(str(s["art"]), "test-gpu", uv=None, recipes=s["recipes"], ext=ROOT, log=lambda x: None, **kw)


def test_one_recipe_publishes_a_farm_and_a_second_run_reuses_it(setup):
    t = run(setup)
    art = setup["art"]
    assert t["template"] == "test-gpu-1"
    assert t["gpu"] == {"core": "comfyui-0.41.0", "venv": "gv-1", "nodes": ["farm-gpu-1"], "flags": [],
                        "env": {"HSA_TOOLS_DISABLE_REGISTER": "1"}}
    assert t["ui"]["venv"] == "uv-1" and t["ui"]["nodes"] == ["farm-ui-1"] and t["ui"]["flags"] == ["--cpu"]
    assert sorted(os.listdir(art / "nodes" / "farm-ui-1")) == ["READY", "SomeNode", "comfyfarm_ui", "comfyfarm_worker",
                                                                   "manifest.json"]
    assert sorted(os.listdir(art / "nodes" / "farm-gpu-1")) == ["READY", "SomeNode", "comfyfarm_worker", "manifest.json"]
    assert (art / "core" / "comfyui-0.41.0" / "READY").exists()
    assert [b[1] for b in setup["built"]][0] == ["torch\n", "torchaudio\n", "imageio-ffmpeg\n"]   # core, base, nodes
    assert any(a.startswith("--index-url=https://example.invalid/rocm") for a in setup["built"][0][2])
    assert json.loads((art / "profiles" / "test-gpu-1.json").read_text())["gpu"] == t["gpu"]
    assert run(setup) == t and len(setup["built"]) == 2                                   # nothing new, no build

    # a node at another commit: new node sets and template; the venvs (same requirements) stay
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.invalid", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x.invalid"}
    (setup["node"] / "extra.py").write_text("X = 1\n")
    subprocess.run(["git", "-C", str(setup["node"]), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(setup["node"]), "commit", "-qm", "y"], check=True, env=git_env)
    setup["recipe"]["nodes"][0]["commit"] = subprocess.run(["git", "-C", str(setup["node"]), "rev-parse", "HEAD"],
                                                           capture_output=True, text=True).stdout.strip()
    (setup["rdir"] / "test-gpu.json").write_text(json.dumps(setup["recipe"]))
    t2 = run(setup)
    assert t2["template"] == "test-gpu-2" and t2["gpu"]["nodes"] == ["farm-gpu-2"] and t2["gpu"]["venv"] == "gv-1"
    assert len(setup["built"]) == 2
    t3 = run(setup, rebuild=True)
    assert t3["gpu"]["venv"] == "gv-2" and t3["ui"]["venv"] == "uv-2" and t3["template"] == "test-gpu-3"


def test_a_change_that_keeps_every_file_size_is_still_a_change(setup):
    """tree_sha256 is path+size (contract: change detection); reuse goes by contents."""
    t = run(setup)
    assert t["gpu"]["nodes"] == ["farm-gpu-1"]
    art = setup["art"]
    m = json.loads((art / "nodes" / "farm-gpu-1" / "manifest.json").read_text())
    assert len(m["content_sha256"]) == 64 and m["sources"]["SomeNode"]["commit"] == setup["recipe"]["nodes"][0]["commit"]
    assert json.loads((art / "core" / "comfyui-0.41.0" / "manifest.json").read_text())["source"]["ref"] == "v0.41.0"
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.invalid", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x.invalid"}
    (setup["node"] / "__init__.py").write_text("NODE_CLASS_MAPPINGS = []\n")           # {} → [], same size
    subprocess.run(["git", "-C", str(setup["node"]), "commit", "-qam", "same size"], check=True, env=git_env)
    setup["recipe"]["nodes"][0]["commit"] = subprocess.run(["git", "-C", str(setup["node"]), "rev-parse", "HEAD"],
                                                           capture_output=True, text=True).stdout.strip()
    (setup["rdir"] / "test-gpu.json").write_text(json.dumps(setup["recipe"]))
    t2 = run(setup)
    assert t2["gpu"]["nodes"] == ["farm-gpu-2"] and t2["template"] == "test-gpu-2"


def test_a_recipe_is_checked_before_anything_is_fetched_or_built(setup):
    r = setup["recipe"]
    bad = [
        ({"comfyui": {"repo": r["comfyui"]["repo"], "ref": "master"}}, "never a branch"),
        ({"comfyui": {"repo": "--upload-pack=sh -c id", "ref": "v0.41.0"}}, "comfyui needs repo"),
        ({"comfyui": {}}, "comfyui needs repo"),
        ({"gpu": {**r["gpu"], "flags": ["--enable-manager"]}}, "flags the launcher refuses"),
        ({"gpu": {**r["gpu"], "env": {"LD_PRELOAD": "x"}}}, "environment the launcher refuses"),
        ({"gpu": {**r["gpu"], "index": "http://plain.invalid/x"}}, "https URL"),
        ({"nodes": [{**r["nodes"][0], "repo": "-oProxyCommand=x"}]}, "a node needs name, repo"),
        ({"nodes": [{**r["nodes"][0], "requirements": ["--index-url=https://evil.invalid"]}]}, "requirement lines"),
    ]
    for change, why in bad:
        (setup["rdir"] / "test-gpu.json").write_text(json.dumps({**r, **change}))
        with pytest.raises(publish.Refused, match=why):
            run(setup)
    assert not (setup["art"] / "core").exists() and not setup["built"]


def test_templates_of_a_recipe_whose_name_starts_another_are_its_own(setup):
    run(setup)
    (setup["rdir"] / "test.json").write_text(json.dumps({**setup["recipe"], "recipe": "test"}))
    t = publish.publish_recipe(str(setup["art"]), "test", uv=None, recipes=setup["recipes"], ext=ROOT, log=lambda x: None)
    assert t["template"] == "test-1" and t["gpu"] == run(setup)["gpu"]           # the artifacts are shared, the template not


def test_a_core_name_with_other_contents_is_refused(setup):
    d = setup["art"] / "core" / "comfyui-0.41.0"
    d.mkdir(parents=True)
    (d / "main.py").write_text("something else\n")
    publish.write_manifest(str(d), "core", "comfyui-0.41.0")
    (d / "READY").write_text("")
    with pytest.raises(publish.Refused, match="other contents"):
        run(setup)


def test_recipes_must_be_pinned(setup):
    setup["recipe"]["nodes"][0]["commit"] = "main"
    (setup["rdir"] / "test-gpu.json").write_text(json.dumps(setup["recipe"]))
    with pytest.raises(publish.Refused, match="full commit"):
        run(setup)
    with pytest.raises(publish.Refused, match="recipe x.json"):                  # a name, not a path
        publish.load_recipe("x.json", setup["recipes"])


def test_the_shipped_recipes_load():
    for f in os.listdir(os.path.join(ROOT, "recipes")):
        r = publish.load_recipe(f[:-5], ROOT)
        assert r["comfyui"]["ref"].startswith("v") and r["gpu"]["accelerator"] in ("cuda", "rocm")


def test_the_shipped_recipes_make_one_farm():
    """A farm has one UI worker for every group: whichever recipe its profile came from, it must run the ComfyUI and the
    nodes every GPU recipe runs. The shipped recipes differ only in their GPU part — and so they also reuse each other's
    core, UI venv and node sets in a shared store."""
    recipes = [publish.load_recipe(f[:-5], ROOT) for f in sorted(os.listdir(os.path.join(ROOT, "recipes")))]
    assert len(recipes) >= 2
    same = lambda r: (r["comfyui"], r.get("python"), r["ui"], r.get("nodes", []), r.get("constraints", []))   # noqa: E731
    assert all(same(r) == same(recipes[0]) for r in recipes), [r["recipe"] for r in recipes]
    assert len({r["gpu"]["venv"] for r in recipes}) == len(recipes)                   # GPU venvs never share a name


def node_repo(s, name, files, submodule=None):
    path = s["tmp"] / name
    commit = git_repo(path, files)
    if submodule:
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.invalid", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x.invalid"}
        subprocess.run(["git", "-C", str(path), "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                        str(submodule), "repositories/sub"], check=True, env=env, capture_output=True)
        subprocess.run(["git", "-C", str(path), "commit", "-qm", "sub"], check=True, env=env)
        commit = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return {"name": name, "repo": str(path), "commit": commit}


def write(s, **change):
    (s["rdir"] / "test-gpu.json").write_text(json.dumps({**s["recipe"], **change}))


def test_a_packs_own_requirements_are_used_without_what_never_comes_from_a_pack(setup):
    """Field analysis of 42 packs: requirements.txt is enough for most — once torch, options and local paths are left
    out and the venv keeps one OpenCV and one onnxruntime."""
    a = node_repo(setup, "PackA", {"__init__.py": "", "requirements.txt": "# deps\nnumpy>=1.25  # trailing\ntorch>=2.0\n"
                                   "torchvision\n--extra-index-url https://evil.invalid/simple\n-e .\nfile:///x\n"
                                   "opencv-python\nsam2 @ git+https://github.com/facebookresearch/sam2\n"})
    b = node_repo(setup, "PackB", {"__init__.py": "", "requirements.txt": "big-lib\n", "requirements-lite.txt": "small-lib\n"})
    c = node_repo(setup, "PackC", {"__init__.py": "", "pyproject.toml": '[project]\nname = "c"\ndependencies = ["lark", "torch"]\n'})
    d = node_repo(setup, "GpuOnly", {"__init__.py": "", "requirements.txt": "gpu-only-lib\n"})
    notes = []
    write(setup, nodes=[a, {**b, "requirements_file": "requirements-lite.txt"}, c, {**d, "ui": False}])
    t = publish.publish_recipe(str(setup["art"]), "test-gpu", uv=None, recipes=setup["recipes"], ext=ROOT, log=notes.append)
    gpu, ui = setup["built"]
    assert gpu[1][2].split("\n")[:-1] == ["numpy>=1.25", "opencv-contrib-python-headless",
                                           "sam2 @ git+https://github.com/facebookresearch/sam2", "small-lib", "lark", "gpu-only-lib"]
    assert "gpu-only-lib" not in ui[1][2] and "small-lib" in ui[1][2]                    # ui: false stays off the UI worker
    assert sorted(os.listdir(setup["art"] / "nodes" / t["ui"]["nodes"][0])) == [
        "PackA", "PackB", "PackC", "READY", "comfyfarm_ui", "comfyfarm_worker", "manifest.json"]
    assert "GpuOnly" in os.listdir(setup["art"] / "nodes" / t["gpu"]["nodes"][0])
    said = "\n".join(notes)
    for left_out in ("'torch>=2.0'", "'torchvision'", "'--extra-index-url", "'-e .'", "'file:///x'", "'torch' in pyproject"):
        assert left_out in said, left_out
    # one OpenCV and the onnxruntime of the accelerator (the GPU venv here is ROCm: the CPU onnxruntime)
    assert "opencv-python; python_version < '0'" in gpu[4][0] and "opencv-contrib-python-headless;" not in gpu[4][0]
    assert "onnxruntime-gpu; python_version < '0'" in gpu[4][0] and "onnxruntime-gpu;" in ui[4][0]
    write(setup, nodes=[{**b, "requirements_file": "requirements-none.txt"}])
    with pytest.raises(publish.Refused, match="no requirements-none.txt"):
        run(setup)
    for bad in ("../x.txt", "/etc/passwd.txt", "req.sh"):
        write(setup, nodes=[{**b, "requirements_file": bad}])
        with pytest.raises(publish.Refused, match="requirements_file"):
            run(setup)


def test_cuda_keeps_the_gpu_onnxruntime():
    o = publish.overrides_for("cuda")
    assert "onnxruntime; python_version < '0'" in o and "onnxruntime-gpu" not in o
    assert publish.KEEP_OPENCV not in o and o.count("opencv") == 3


def test_constraints_reach_the_venv_build_and_are_checked(setup):
    """LTXVideo breaks on kornia 0.8 (its pyramid module lost a function) though it asks for any kornia."""
    write(setup, constraints=["numpy<3"], nodes=[{**setup["recipe"]["nodes"][0], "constraints": ["kornia<0.8"]}])
    run(setup)
    assert setup["built"][0][3] == ["numpy<3\nkornia<0.8\n"]
    for bad in (["--index-url=https://x"], ["kornia<0.8; rm -rf /\n"], [1]):
        write(setup, constraints=bad)
        with pytest.raises(publish.Refused, match="constraints"):
            run(setup)


def test_submodules_come_with_the_pack(setup):
    """Field analysis: a pack whose submodule is missing downloads it when ComfyUI imports it — and fails offline."""
    inner = setup["tmp"] / "Inner"
    inner_commit = git_repo(inner, {"scripts/upscale.py": "X = 1\n"})
    p = node_repo(setup, "WithSub", {"__init__.py": ""}, submodule=inner)
    write(setup, nodes=[p])
    t = run(setup)
    nodes = setup["art"] / "nodes" / t["gpu"]["nodes"][0]
    assert (nodes / "WithSub" / "repositories" / "sub" / "scripts" / "upscale.py").read_text() == "X = 1\n"
    assert not (nodes / "WithSub" / ".git").exists() and not (nodes / "WithSub" / "repositories" / "sub" / ".git").exists()
    m = json.loads((nodes / "manifest.json").read_text())
    assert m["sources"]["WithSub"]["submodules"] == {"repositories/sub": inner_commit}


def test_a_venv_installs_all_its_requirements_at_once(tmp_path, monkeypatch):
    calls = []
    real = subprocess.run

    def fake(argv, *a, **kw):
        if "install" in argv and "pip" in " ".join(argv):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real(argv, *a, **kw)
    monkeypatch.setattr(publish.subprocess, "run", fake)
    (tmp_path / "a.txt").write_text("x\n"); (tmp_path / "b.txt").write_text("y\n"); (tmp_path / "c.txt").write_text("z<2\n")
    with pytest.raises(publish.Refused, match="need uv"):
        publish.publish_venv(str(tmp_path / "art"), "v-1", sys.executable, [str(tmp_path / "a.txt")], "cpu", [], [],
                             overrides=[str(tmp_path / "c.txt")])
    publish.publish_venv(str(tmp_path / "art"), "v-2", sys.executable, [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")],
                         "cpu", [], ["--quiet"], constraints=[str(tmp_path / "c.txt")])
    assert len(calls) == 1
    argv = calls[0]
    assert argv[argv.index("-r") + 1].endswith("a.txt") and argv.count("-r") == 2 and argv[argv.index("-c") + 1].endswith("c.txt")


def test_a_dropped_connection_is_tried_again(setup, monkeypatch):
    """Field: one of 42 fetches died with a TLS read error and took the whole build with it."""
    monkeypatch.setattr(publish, "FETCH_WAIT", 0)
    real, seen = subprocess.run, []

    def flaky(argv, *a, **kw):
        if "fetch" in argv and not seen:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 128, "", "error: RPC failed; curl 56 GnuTLS recv error (-24)")
        return real(argv, *a, **kw)
    monkeypatch.setattr(publish.subprocess, "run", flaky)
    assert run(setup)["template"] == "test-gpu-1" and seen
    monkeypatch.setattr(publish.subprocess, "run", lambda argv, *a, **kw: subprocess.CompletedProcess(argv, 128, "", "down")
                        if "fetch" in argv else real(argv, *a, **kw))
    setup["recipe"]["comfyui"]["ref"] = "v0.41.0"
    with pytest.raises(subprocess.CalledProcessError):
        publish.fetch(setup["recipe"]["comfyui"]["repo"], "v0.41.0", str(setup["tmp"] / "x"))


def test_the_dependency_check_knows_what_the_overrides_removed(tmp_path, monkeypatch):
    """Field build of 42 packs: eight packages ask for OpenCV variants the overrides removed, and decord's wheel says
    cp36 in its metadata yet runs — none of that may stop a build; a real missing dependency must."""
    o = tmp_path / "o.txt"
    o.write_text(publish.overrides_for("cuda"))
    out = ("Found 3 incompatibilities\n"
           "The package `mediapipe` requires `opencv-contrib-python`, but it's not installed\n"
           "The package `albumentations` requires `opencv-python-headless>=4.9.0.80`, but it's not installed\n"
           "The package `decord` was built for a different platform\n")
    monkeypatch.setattr(publish.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, out, ""))
    publish.check_dependencies(["uv", "pip", "check"], [str(o)])
    bad = out + "The package `pixeloe` requires `numpy`, but it's not installed\n"
    monkeypatch.setattr(publish.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, bad, ""))
    with pytest.raises(publish.Refused, match="pixeloe"):
        publish.check_dependencies(["uv", "pip", "check"], [str(o)])
    pip_style = "foo 1.0 requires onnxruntime, which is not installed.\n"      # onnxruntime is removed on CUDA
    monkeypatch.setattr(publish.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, pip_style, ""))
    publish.check_dependencies(["pip", "check"], [str(o)])
    with pytest.raises(publish.Refused, match="foo 1.0 requires onnxruntime"):
        publish.check_dependencies(["pip", "check"], [])                       # without overrides it is missing


def test_the_kept_opencv_and_onnxruntime_are_asked_for(tmp_path, monkeypatch):
    """Field build: the overrides removed every other OpenCV and nothing asked for the kept one — no cv2 at all, and
    eight packs failed to import. A pack's own line becomes the kept package; a dependency's is found by resolving."""
    assert publish.one_of(["opencv-python>=4.7.0", "opencv-contrib-python", "onnxruntime-gpu>=1.15", "onnxruntime",
                           "onnxruntime-directml", "opencv-pythonic", "numpy"], "rocm") == [
        "opencv-contrib-python-headless>=4.7.0", "opencv-contrib-python-headless", "onnxruntime>=1.15", "onnxruntime",
        "onnxruntime-directml", "opencv-pythonic", "numpy"]
    assert publish.one_of(["onnxruntime"], "cuda") == ["onnxruntime-gpu"]
    compiled = "# via mediapipe\nmediapipe==1.1.1\nopencv-contrib-python==5.0.0.93\n    # via mediapipe\nnumpy==2.5.4\n"
    monkeypatch.setattr(publish.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, compiled, ""))
    assert publish.needed_through_dependencies("uv", ["r.txt"], [], [], "3.12", "cuda") == ["opencv-contrib-python-headless"]
    monkeypatch.setattr(publish.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "numpy==2\n", ""))
    assert publish.needed_through_dependencies("uv", ["r.txt"], [], [], "3.12", "cuda") == []
    monkeypatch.setattr(publish.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "no"))
    assert publish.needed_through_dependencies("uv", ["r.txt"], [], [], "3.12", "cpu") == ["opencv-contrib-python-headless",
                                                                                           "onnxruntime"]


def test_one_half_and_packs_from_the_dashboard(setup):
    """The control-plane builder: the first start builds the UI half only; later builds add the dashboard's packs."""
    t = publish.publish_recipe(str(setup["art"]), "test-gpu", uv=None, recipes=setup["recipes"], ext=ROOT,
                               log=lambda x: None, roles=("ui",))
    assert "gpu" not in t and t["ui"]["venv"] == "uv-1" and [b[0] for b in setup["built"]] == ["uv-1"]
    extra = node_repo(setup, "DashPack", {"__init__.py": "", "requirements.txt": "dash-lib\n"})
    t2 = publish.publish_recipe(str(setup["art"]), "test-gpu", uv=None, recipes=setup["recipes"], ext=ROOT,
                                log=lambda x: None, extra_nodes=[extra])
    assert t2["template"] != t["template"] and "gpu" in t2 and "dashboard" in t2["about"]
    assert "DashPack" in os.listdir(setup["art"] / "nodes" / t2["gpu"]["nodes"][0])
    assert "dash-lib" in setup["built"][-1][1][2]
    with pytest.raises(publish.Refused, match="unique"):                         # a pack may not shadow the recipe's
        publish.publish_recipe(str(setup["art"]), "test-gpu", uv=None, recipes=setup["recipes"], ext=ROOT,
                               log=lambda x: None, extra_nodes=[{**extra, "name": "SomeNode"}])
    with pytest.raises(publish.Refused, match="roles"):
        publish.publish_recipe(str(setup["art"]), "test-gpu", uv=None, recipes=setup["recipes"], ext=ROOT,
                               log=lambda x: None, roles=("cpu",))


def test_the_platform_reaches_the_install_only_with_uv(setup):
    import shutil
    uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
    if os.path.exists(uv):                                                       # the real uv installs the interpreter
        publish.publish_recipe(str(setup["art"]), "test-gpu", uv=uv, recipes=setup["recipes"], ext=ROOT,
                               log=lambda x: None, platform="x86_64-manylinux_2_35")
        assert "--python-platform=x86_64-manylinux_2_35" in setup["built"][0][2]
    with pytest.raises(publish.Refused, match="platform"):
        publish.publish_recipe(str(setup["art"]), "test-gpu", uv=None, recipes=setup["recipes"], ext=ROOT,
                               log=lambda x: None, platform="x86; rm")



def test_a_pack_may_name_several_requirements_files(setup):
    """Field (CUDA, 42 packs): a pack that keeps a requirements file per node (AlekPet) pip-installed them while
    ComfyUI imported it; named here, they are in the venv instead."""
    p = node_repo(setup, "PerNode", {"__init__.py": "", "A/requirements.txt": "lib-a\n", "B/requirements.txt": "lib-b\n"})
    write(setup, nodes=[{**p, "requirements_file": "A/requirements.txt, B/requirements.txt"}])
    run(setup)
    assert setup["built"][0][1][2] == "lib-a\nlib-b\n"
    write(setup, nodes=[{**p, "requirements_file": "A/requirements.txt,../x.txt"}])
    with pytest.raises(publish.Refused, match="requirements_file"):
        run(setup)


def test_uv_copies_into_the_store(monkeypatch):
    """Field (dev.13): every control-plane build warned "Failed to hardlink files; falling back to full copy" — uv's
    cache and the store are separate mounts there. A store's venv is its own copy anyway: copy is the mode, unless the
    operator says otherwise."""
    env = {}
    monkeypatch.setattr(publish.os, "environ", env)
    with pytest.raises(SystemExit):
        publish.main(["venv"])                                  # stops at the arguments; the default is set before
    assert env["UV_LINK_MODE"] == "copy"
    env.clear(); env["UV_LINK_MODE"] = "symlink"
    with pytest.raises(SystemExit):
        publish.main(["venv"])
    assert env["UV_LINK_MODE"] == "symlink"
