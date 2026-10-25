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


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_progress_is_weighed_by_the_plan_and_paced_by_the_sampler():
    """One KSampler of 20 steps among 10 other nodes: at step 5 the job is about a quarter done, not 95 %; the ETA is
    the steps left at the sampler's pace; a job the worker stopped reporting is told apart from one at 0 %."""
    mod = (ROOT / "src" / "comfyfarm" / "dashboard" / "js" / "progress.js").as_uri()
    script = f"""
import {{ weighted, pace, eta, silence }} from "{mod}";
const plan = {{ "3": 20 }}; for (let i = 10; i < 20; i++) plan[String(i)] = 0.1;
const state = {{ "3": {{ value: 5, max: 20, state: "running" }} }};
for (let i = 10; i < 20; i++) state[String(i)] = {{ value: 1, max: 1, state: "finished" }};
const w = weighted({{ state, at: 1000 }}, plan);
const naive = Object.values(state).filter((n) => n.state === "finished").length / Object.keys(state).length;
const samples = [{{ at: 1000, id: "3", value: 5, max: 20 }}, {{ at: 1030, id: "3", value: 8, max: 20 }}];
const w2 = weighted({{ state: {{ ...state, "3": {{ value: 8, max: 20, state: "running" }} }}, at: 1030 }}, plan);
const out = {{
  frac: w.frac, naive, remaining: w.remaining, current: w.current,
  pace: pace(samples), eta: eta(w2, pace(samples)),
  etaNoPace: eta(w, null), etaBeforeFirstStep: eta(weighted({{ state: {{ "3": {{ value: 0, max: 20, state: "running" }} }}, at: 1 }}, plan), 10),
  unplanned: weighted({{ state: {{ "3": {{ value: 10, max: 20, state: "running" }}, "40:2": {{ value: 1, max: 1, state: "finished" }} }} }}, {{ "3": 20, "40": 5 }}).frac,
  nothing: weighted({{ state: {{}} }}, plan), bare: weighted({{ state: {{ value: 3, max: 6, node: "3" }} }}, plan).frac,
  quiet: silence({{ at: 1000 }}, 1045), noReport: silence({{}}, 1045),
}};
console.log(JSON.stringify(out));
"""
    o = json.loads(subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert 0.27 < o["frac"] < 0.30 and o["naive"] > 0.9                   # (1 + 5) / 21 against 10 of 11 nodes done
    assert abs(o["remaining"] - 15) < 1e-9 and o["current"] == {"id": "3", "value": 5, "max": 20}
    assert o["pace"] == 10 and abs(o["eta"] - 120) < 1e-9                    # 3 steps in 30 s; 12 steps left
    assert o["etaNoPace"] is None and abs(o["etaBeforeFirstStep"] - 210) < 1e-9   # paced: 20 steps + the ten small nodes, × 10 s
    assert abs(o["unplanned"] - 15 / 25) < 1e-9                            # "40:2" weighs as its parent 40
    assert o["nothing"]["frac"] is None and o["nothing"]["started"] is False and o["bare"] == 0.5
    assert o["quiet"] == 45 and o["noReport"] is None


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
