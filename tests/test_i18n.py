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
        for table in ("STATUS", "TABS", "OSES", "THEMES", "SETTING_LABELS", "PATHS"):  # [value, "English label"]
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
