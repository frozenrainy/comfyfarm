"""Every text the dashboard passes through t() has a Korean entry (a missing one would silently show English)."""
import pathlib
import re

DASH = pathlib.Path(__file__).parent.parent / "src" / "comfyfarm" / "dashboard"
LITERAL = r'"((?:[^"\\]|\\.)*)"'


def used_keys() -> set[str]:
    keys = set()
    for f in [*DASH.glob("js/**/*.js"), DASH / "app.js", DASH / "index.html"]:
        if "lang" in f.parts:
            continue
        s = f.read_text()
        for m in re.finditer(r"\bt\(\s*" + LITERAL + r"((?:\s*\+\s*" + LITERAL + r")*)", s):
            keys.add(m.group(1) + "".join(re.findall(LITERAL, m.group(2) or "")))
        keys |= set(re.findall(r'data-i18n(?:-title)?="([^"]*)"', s))
        for m in re.finditer(r"BACKEND_HINT\s*=\s*\{(.*?)\};", s, re.S):            # [example, "English explanation"]
            keys |= set(re.findall(r"'[^']*',\s*" + LITERAL, m.group(1)))
        for table in ("STATUS", "TABS", "OSES", "THEMES", "SETTING_LABELS", "PATHS", "GPUS"):  # [value, "English label"]
            for m in re.finditer(table + r"\s*=\s*([\[{].*?[\]}]);", s, re.S):
                keys |= {b for a, b in re.findall(LITERAL + r"\s*[,:]\s*" + LITERAL, m.group(1))}
    return keys


def ko_keys() -> set[str]:
    s = (DASH / "js" / "lang" / "ko.js").read_text()
    return set(re.findall(LITERAL + r"\s*:", s))          # every "English": "한국어" pair's key


def test_every_dashboard_text_has_a_korean_entry():
    missing = sorted(k for k in used_keys() - ko_keys() if re.search("[A-Za-z]", k) and k not in ("Linux/macOS",))
    assert len(used_keys()) > 150 and len(ko_keys()) > 150             # the scan itself found the texts
    assert not missing, f"add to dashboard/js/lang/ko.js: {missing}"


def test_korean_table_has_no_duplicate_keys():
    s = (DASH / "js" / "lang" / "ko.js").read_text()
    keys = re.findall(LITERAL + r"\s*:", s)
    assert len(keys) == len(set(keys)), sorted({k for k in keys if keys.count(k) > 1})


def test_translated_worker_warnings_still_match_what_the_router_says():
    """admin.js translates these router sentences by exact text: a reworded sentence would silently stay English."""
    src = (DASH.parent / "api" / "admin.py").read_text() + (DASH.parent / "core" / "consistency.py").read_text()
    js = (DASH / "js" / "pages" / "admin.js").read_text()
    block = js[js.index("function warnText"):js.index("const head")]
    for sentence in set(re.findall(r'\[?\s*"((?:[^"\\]|\\.)*)"\s*[:,]\s*\n?\s*t\(', block)):
        assert sentence.rstrip(" :") in src.replace('"\n                   + (', ''), sentence


def test_every_host_check_line_has_a_korean_entry():
    """hostcheck.py sends each line's English template (Msg) and the line names; the dashboard translates them at run
    time, so the scan of t("…") above never sees them."""
    import ast
    tree = ast.parse((DASH.parent / "hostcheck.py").read_text())
    templates = {n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "Msg"
                 and n.args and isinstance(n.args[0], ast.Constant)}
    names = {n.args[1].value for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "add"
             and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant)}
    names |= {f"{k} directory" for k in ("input", "output", "user", "temp")}
    assert len(templates) > 40 and len(names) > 15
    missing = sorted((templates | names) - ko_keys())
    assert not missing, f"add to dashboard/js/lang/ko.js: {missing}"


def test_translated_warnings_with_an_address_still_match_the_router():
    """admin.js reads the address out of these two router sentences with a pattern: a reworded one stays English."""
    src = (DASH.parent / "api" / "admin.py").read_text()
    js = (DASH / "js" / "pages" / "admin.js").read_text()
    assert "/^refuses the router: its allow-list does not have (.+?) — add that address/" in js
    assert "refuses the router: its allow-list does not have {seen} — add that address to" in src
    assert r"/^refused (\d+) request\(s\), the last from (.+?) — if that is/" in js
    assert "request(s), the last from {ref.get('last_peer')} — if that is" in src
