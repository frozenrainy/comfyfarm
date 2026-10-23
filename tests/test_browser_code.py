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
