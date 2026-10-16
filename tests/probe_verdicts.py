"""Normalise protocol-probe output to comparable verdicts (status per check + the few INFO facts that matter)."""
import re


def verdicts(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        m = re.match(r"\[(PASS|FAIL|INFO)\] (S\d+[a-z]?)\b[^:]*: (.*)", line)
        if not m:
            continue
        status, tag, detail = m.groups()
        if tag == "S3":
            n = int(re.search(r"received (\d+)", detail).group(1))
            out[tag] = "events" if n > 0 else "no-events"
        elif tag in ("S1c", "S1d") and "status" in detail:
            out.setdefault(tag, re.search(r"status (\d+)", detail).group(1))
        elif status != "INFO":
            out[tag] = status
    return out
