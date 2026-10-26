"""The browser code on a farm opened at http://<address>: not a secure context, so no crypto.randomUUID and no
navigator.clipboard (field, dev.19: the first Run of a new farm failed with "crypto.randomUUID is not a function")."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXT = ROOT / "ui_ext" / "comfyfarm_ui" / "web" / "comfyfarm.js"
BROWSER = [EXT, *sorted((ROOT / "src" / "comfyfarm" / "dashboard").rglob("*.js"))]


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_a_submission_gets_its_id_without_a_secure_context():
    src = EXT.read_text()
    fn = re.search(r"function uuid4\(\) \{.*?\n\}", src, re.S).group(0)
    script = fn + """
const real = globalThis.crypto;
Object.defineProperty(globalThis, "crypto", { value: { getRandomValues: (a) => real.getRandomValues(a) }, configurable: true });
const ids = Array.from({ length: 200 }, uuid4);
console.log(JSON.stringify(ids));
"""
    ids = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    v4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    assert all(v4.match(i) for i in ids) and len(set(ids)) == len(ids)


PROGRESS = (ROOT / "src" / "comfyfarm" / "dashboard" / "js" / "progress.js").as_uri()


def run_progress(body: str):
    script = f'import {{ weighted, pace, eta, silence, promptNode, copyOf }} from "{PROGRESS}";\nimport fs from "fs";\n{body}'
    return json.loads(subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True,
                                     cwd=ROOT).stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_progress_is_weighed_by_the_plan_and_paced_by_the_sampler():
    """One KSampler of 20 steps among 10 other nodes: at step 5 the job is about a quarter done, not 95 %; the time left
    is the steps left at the sampler's pace — from the router's first step report, the model's loading left out."""
    o = run_progress("""
const plan = { "3": 20 }; for (let i = 10; i < 20; i++) plan[String(i)] = 0.1;
const state = { "3": { value: 5, max: 20, state: "running" } };
for (let i = 10; i < 20; i++) state[String(i)] = { value: 1, max: 1, state: "finished" };
const progress = { state, at: 1300, steps: { "3": [1000, 2] } };          // step 2 at 1000, step 5 at 1300: 100 s a step
const w = weighted(progress, plan);
const naive = Object.values(state).filter((n) => n.state === "finished").length / Object.keys(state).length;
const p = pace(progress, w.current);
console.log(JSON.stringify({
  frac: w.frac, naive, current: w.current, pace: p, eta: eta(w, p),
  paceFromPage: pace({ state, at: 1300 }, w.current, [{ at: 1000, id: "3", value: 5, max: 20 }, { at: 1030, id: "3", value: 8, max: 20 }]),
  steps20: weighted({ state: { "3": { value: 1, max: 40, state: "running" } } }, { "3": 20 }).frac,  // ComfyUI's max wins
  nothing: weighted({ state: {} }, plan), bare: weighted({ state: { value: 3, max: 6, node: "3" } }, plan).frac,
}));
""")
    assert 0.27 < o["frac"] < 0.30 and o["naive"] > 0.9                   # (1 + 5) / 21 against 10 of 11 nodes done
    assert o["current"] == {"id": "3", "value": 5, "max": 20} and o["pace"] == 100
    assert abs(o["eta"] - 1500) < 1e-6                                     # 15 steps left (14 + ten 0.1s) × 100 s
    assert o["paceFromPage"] == 10 and o["steps20"] == 1 / 40
    assert o["nothing"]["frac"] is None and o["nothing"]["started"] is False and o["bare"] == 0.5


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_a_long_step_is_not_a_silent_worker():
    """dev.21 said "no word" 20 s into every 208-second step. Silence is measured against the job's own pace."""
    o = run_progress("""
const p = { at: 1000 };
console.log(JSON.stringify({
  midStep: silence(p, 1196, 208), longStep: silence(p, 1000 + 2.5 * 208 + 1, 208),
  loading: silence(p, 1500, null), stuckLoading: silence(p, 1601, null), short: silence(p, 1121, 2), none: silence({}, 5, 1),
}));
""")
    assert o["midStep"] == {"quiet": 196, "alarming": False}
    assert o["longStep"]["alarming"] is True
    assert o["loading"]["alarming"] is False and o["stuckLoading"]["alarming"] is True     # 10 min before the first step
    assert o["short"]["alarming"] is True                                  # fast steps: still at least 2 min of grace
    assert o["none"] == {"quiet": None, "alarming": False}


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_a_real_loop_run_is_weighed_by_its_iterations():
    """A real run (field, dev.21), ids and numbers only: a VACE workflow whose StartLoop 96 runs its 57-node body 6
    times; iteration 1, the high-noise sampler 96.0.0.0_160:137 at 7/10. dev.21 showed 3.3 %: the copies' ids were
    not the plan's, so the sampler weighed 0.1. About 40 minutes into a 4-hour run is near 9 %."""
    o = run_progress("""
const f = JSON.parse(fs.readFileSync("tests/fixtures/progress_vace_loop.json"));
const now = f.progress.at + 60;
const w = weighted(f.progress, f.plan, f.loops, now);
// the same run once the router has heard "Iteration 0 / 6" at t0 and "Iteration 1 / 6" 40 min later
const t0 = f.progress.at - 2000, said = { ...f.progress, loops: { "96": { n: 6, ends: [t0, t0 + 2400] } } };
const w2 = weighted(said, f.plan, f.loops, t0 + 3000);
console.log(JSON.stringify({ frac: w.frac, loop: w.loop, current: w.current, eta: eta(w, 208),
                             frac2: w2.frac, loop2: w2.loop, eta2: eta(w2, 208),
                             ids: [promptNode("96.0.0.0_160:137"), copyOf("96.0.0.5_160:127"), copyOf("160:137")] }));
""")
    assert 0.07 < o["frac"] < 0.12
    assert o["loop"] == {"k": 1, "n": 6, "known": False}                  # 6 seen in the copies' ids, not yet said
    assert o["current"] == {"id": "96.0.0.0_160:137", "value": 7, "max": 10}
    assert o["eta"] is None                                                # a first iteration is not timed yet: no number
    assert o["loop2"] == {"k": 2, "n": 6, "known": True} and 1 / 6 <= o["frac2"] < 2 / 6
    assert abs(o["eta2"] - ((2400 - 600) + 4 * 2400)) < 1e-6               # the rest of this one + four more, 40 min each
    assert o["ids"] == ["160:137", {"start": "96", "pos": 5}, None]


def test_no_browser_code_needs_a_secure_context():
    """crypto.randomUUID and navigator.clipboard only behind a check that has another way."""
    for f in BROWSER:
        text = f.read_text()
        for n, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("//"):
                continue
            if "crypto.randomUUID" in line:
                assert 'typeof crypto.randomUUID === "function"' in line, f"{f.name}:{n}: {line.strip()}"
            if "navigator.clipboard" in line:          # only where the file copies the old way too
                assert 'execCommand("copy")' in text, f"{f.name}:{n}: navigator.clipboard without a fallback"
            assert "crypto.subtle" not in line, f"{f.name}:{n}: crypto.subtle needs a secure context"


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_editor_says_when_its_node_list_is_out_of_date():
    """D-46: /object_info served out of date carries the time it was read; the extension says so once per such list,
    with a toast — packs added since show as missing otherwise, for no visible reason."""
    src = EXT.read_text()
    fn = re.search(r"let staleWarned = \"\";\nfunction warnIfStale\(res\) \{.*?\n\}", src, re.S).group(0)
    script = """
const toasts = [];
const app = { extensionManager: { toast: { add: (t) => toasts.push(t) } } };
const tr = (s) => s;
""" + fn + """
const res = (h) => ({ headers: { get: (k) => (k === "X-ComfyFarm-Nodes-Stale" ? h : null) } });
warnIfStale(res(null));                                   // up to date: nothing
warnIfStale(res("2026-10-26T01:30:56+00:00"));
warnIfStale(res("2026-10-26T01:30:56+00:00"));            // the same list again (the editor asks twice): once
warnIfStale(res("2026-10-26T09:00:00+00:00"));            // another out-of-date list: again
console.log(JSON.stringify(toasts.map((t) => [t.severity, t.summary, /no worker of this group/.test(t.detail), t.detail.includes("{when}")])));
"""
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out == [["warn", "Node list out of date", True, False]] * 2
    assert "if (/^\\/(api\\/)?object_info$/.test(route)) warnIfStale(res);" in src      # the node list the editor loads


UPLOAD_LIST = (ROOT / "src" / "comfyfarm" / "dashboard" / "js" / "upload_list.js").as_uri()


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_upload_list_keeps_the_next_batch_and_drops_the_last():
    """Field (dev.24): after a batch the done rows stayed; picking the next files added them below, and Clear — the only
    way to remove done rows — took the new picks too. Done rows go when their batch ends; Clear takes what is over."""
    script = f'import {{ clearOver, dropDone, isOver, isWaiting }} from "{UPLOAD_LIST}";' + """
const row = (name, state) => ({ file: { name }, state });
const names = (l) => l.map((x) => `${x.file.name}:${x.state}`);
const out = {};
const list = [row("a", "done"), row("b", "failed"), row("c", "cancelled")];
dropDone(list);                                              // the batch ended
out.afterBatch = names(list);
list.push(row("d", "staged"), row("e", "staged"));           // the next picks
out.sent = names(list.filter(isWaiting));                    // what "Upload all" sends: picks, and the failed one again
out.cleared = clearOver(list);
out.afterClear = names(list);
list.push(row("f", "uploading"));
out.over = list.some(isOver);
out.clearedNone = clearOver(list);
out.left = names(list);
console.log(JSON.stringify(out));
"""
    o = json.loads(subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True,
                                  check=True, cwd=ROOT).stdout)
    assert o["afterBatch"] == ["b:failed", "c:cancelled"]
    assert o["sent"] == ["b:failed", "d:staged", "e:staged"]
    assert o["cleared"] == 2 and o["afterClear"] == ["d:staged", "e:staged"]
    assert o["over"] is False and o["clearedNone"] == 0 and o["left"] == ["d:staged", "e:staged", "f:uploading"]
    models = (ROOT / "src" / "comfyfarm" / "dashboard" / "js" / "pages" / "models.js").read_text()
    assert "dropDone(staged)" in models and "clearOver(staged)" in models
    assert "staged.splice(i, 1); renderUploads(); }), " not in models     # the old Clear (everything not uploading)
    # the groups ticked are this batch's: taken at its start, not read again while it runs
    assert "const grp = (batchGroups || []).map(Number);\n    batchGroups = null;" in models
