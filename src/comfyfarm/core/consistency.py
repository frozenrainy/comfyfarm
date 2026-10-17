"""Group consistency (docs/design/06, 10): a job is validated on one worker of its group and executed on another, so
the farm assumes every worker of a group has the same nodes and models. That assumption is now checked, not trusted:
each worker's inventory (node class names from /object_info, file lists from /models/{folder}) is hashed and compared
with the rest of its group. The majority is the reference; the odd ones out get a warning naming what differs.
"""
from __future__ import annotations

import hashlib
from collections import Counter

SHOW = 5    # names listed in a warning before "…"


def hashes(nodes: list[str], models: dict[str, list[str]]) -> tuple[str, str]:
    n = hashlib.sha256("\n".join(sorted(nodes)).encode()).hexdigest()
    m = hashlib.sha256("\n".join(f"{f}/{x}" for f in sorted(models) for x in sorted(models[f])).encode()).hexdigest()
    return n, m


def _diff(ref: list[str], mine: list[str]) -> tuple[list[str], list[str]]:
    r, m = set(ref), set(mine)
    return sorted(r - m), sorted(m - r)


def report(workers: list[dict]) -> dict:
    """workers: rows with id, group_id, state, enabled, maintenance, capabilities. Returns
    {group_id: {"reference": worker_id | None, "inconsistent": n, "workers": {id: {...}}}} for workers that have an
    inventory. A group with fewer than two inventoried workers has nothing to compare and is consistent."""
    groups: dict = {}
    for w in workers:
        inv = (w.get("capabilities") or {}).get("inventory")
        if w["group_id"] is None or not inv or not w["enabled"] or w["state"] != "online":
            continue
        groups.setdefault(w["group_id"], []).append((w, inv))
    out = {}
    for gid, members in groups.items():
        # the reference: the most common (nodes, models) pair among workers in service; ties → lowest worker id
        eligible = [(w, inv) for w, inv in members if not w["maintenance"]] or members
        counts = Counter((inv["nodes_hash"], inv["models_hash"]) for _, inv in eligible)
        best = max(counts.values())
        ref_key = min(k for k, v in counts.items() if v == best)
        ref_w, ref_inv = min(((w, inv) for w, inv in eligible if (inv["nodes_hash"], inv["models_hash"]) == ref_key),
                             key=lambda x: x[0]["id"])
        entry = {"reference": ref_w["id"], "inconsistent": 0, "workers": {}}
        for w, inv in members:
            same = (inv["nodes_hash"], inv["models_hash"]) == ref_key
            d = {"consistent": same}
            if not same:
                d["nodes_missing"], d["nodes_extra"] = _diff(ref_inv.get("nodes", []), inv.get("nodes", []))
                d["models_missing"], d["models_extra"] = {}, {}
                for folder in sorted(set(ref_inv.get("models", {})) | set(inv.get("models", {}))):
                    miss, extra = _diff(ref_inv.get("models", {}).get(folder, []), inv.get("models", {}).get(folder, []))
                    if miss:
                        d["models_missing"][folder] = miss
                    if extra:
                        d["models_extra"][folder] = extra
                entry["inconsistent"] += 1
            entry["workers"][w["id"]] = d
        out[gid] = entry
    return out


def _names(items: list[str]) -> str:
    return ", ".join(items[:SHOW]) + (f", … (+{len(items) - SHOW})" if len(items) > SHOW else "")


def warnings(d: dict | None) -> list[str]:
    """Human sentences for one worker's entry of report()."""
    if not d or d.get("consistent", True):
        return []
    out = []
    if d.get("nodes_missing"):
        out.append(f"differs from its group: {len(d['nodes_missing'])} node(s) missing — {_names(d['nodes_missing'])}")
    if d.get("nodes_extra"):
        out.append(f"differs from its group: {len(d['nodes_extra'])} extra node(s) — {_names(d['nodes_extra'])}")
    for folder, files in d.get("models_missing", {}).items():
        out.append(f"differs from its group: {len(files)} model(s) missing in {folder} — {_names(files)}")
    for folder, files in d.get("models_extra", {}).items():
        out.append(f"differs from its group: {len(files)} extra model(s) in {folder} — {_names(files)}")
    return out or ["differs from its group (inventory hashes differ)"]
