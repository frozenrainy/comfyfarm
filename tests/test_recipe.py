"""publish.py recipe: one command publishes what a farm needs and a profile template; a second run reuses everything."""
import json
import os
import subprocess

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

    def fake_venv(art, name, python, reqs, acc, arch, pip, uv=None, manifest_extra=None):   # a real one is GBs
        built.append((name, [open(r).read() for r in reqs], pip))
        d = os.path.join(art, "venv", name)
        os.makedirs(d)
        m = publish.write_manifest(d, "venv", name, accelerator=acc, **(manifest_extra or {}))
        open(os.path.join(d, "READY"), "w").close()
        return m
    monkeypatch.setattr(publish, "publish_venv", fake_venv)
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
    assert [b[1] for b in setup["built"]][0] == ["torch\n", "imageio-ffmpeg\n"]          # core + node requirements
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
