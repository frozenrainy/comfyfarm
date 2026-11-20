#!/usr/bin/env python3
"""comfyfarm-ctl — run a ComfyFarm from a terminal or a program, through its API (docs/design/19).

Standard library only (Python 3.10+): installed with the package as `comfyfarm-ctl`, or copied and run as
`python3 ctl.py`. It acts as the user an API token belongs to (Settings → API tokens in the dashboard), with the same
checks and audit trail as the dashboard.

  comfyfarm-ctl login --url https://farm.example        # token read from stdin, saved 0600
  comfyfarm-ctl status
  comfyfarm-ctl hold --reason "driver update" --for 2h --wait
  comfyfarm-ctl release
  comfyfarm-ctl jobs list --all --json
  comfyfarm-ctl rollouts start --profile farm-gpu-26 --group gpu --wait --yes
  comfyfarm-ctl builds start rocm-gfx1201 --wait
  comfyfarm-ctl commands --json                         # every command and its arguments, for programs

Exit codes: 0 ok · 1 refused or failed · 2 usage · 3 not authenticated / not allowed · 4 conflict · 5 not found ·
6 farm unavailable.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

OK, FAILED, USAGE, AUTH, CONFLICT, NOT_FOUND, UNAVAILABLE = 0, 1, 2, 3, 4, 5, 6
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "comfyfarm", "ctl.json")
TIMEOUT = 30


class Fail(Exception):
    def __init__(self, code: int, message: str, body=None):
        super().__init__(message)
        self.code, self.body = code, body


def status_code_exit(status: int) -> int:
    if status in (401, 403):
        return AUTH
    if status == 404:
        return NOT_FOUND
    if status == 409:
        return CONFLICT
    if status >= 500:
        return UNAVAILABLE
    return FAILED


# ---- connection ---------------------------------------------------------------------------------------------------
def load_config(path: str = CONFIG) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError:
        raise Fail(USAGE, f"{path} is not JSON — remove it and log in again")


def save_config(cfg: dict, path: str = CONFIG) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(path + ".tmp", path)


class Farm:
    def __init__(self, url: str, token: str, timeout: float = TIMEOUT):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    def call(self, method: str, path: str, body=None, query: dict | None = None):
        q = {k: v for k, v in (query or {}).items() if v is not None}
        url = self.url + path + ("?" + urllib.parse.urlencode(q) if q else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                doc = json.loads(raw)
            except ValueError:
                doc = {"error": raw.decode("utf-8", "replace")[:500]}
            msg = doc.get("detail") or doc.get("error") or f"HTTP {e.code}"
            raise Fail(status_code_exit(e.code), f"{method} {path}: {e.code} {msg if isinstance(msg, str) else json.dumps(msg)}",
                       doc)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise Fail(UNAVAILABLE, f"cannot reach {self.url}: {getattr(e, 'reason', e)}")
        return json.loads(raw) if raw else None


def connect(args) -> Farm:
    cfg = load_config(args.config)
    url = args.url or os.environ.get("COMFYFARM_URL") or cfg.get("url")
    token = os.environ.get("COMFYFARM_TOKEN") or cfg.get("token")
    if not url:
        raise Fail(USAGE, "no farm: give --url, set COMFYFARM_URL, or run `comfyfarm-ctl login --url …`")
    if not token:
        raise Fail(USAGE, "no token: set COMFYFARM_TOKEN or run `comfyfarm-ctl login` (Settings → API tokens makes one)")
    return Farm(url, token, args.timeout)


# ---- output -------------------------------------------------------------------------------------------------------
def out(args, doc, text: str | None = None):
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=None if args.compact else 1))
    elif text is not None:
        print(text)


def table(rows: list[list], head: list[str]) -> str:
    cells = [head] + [["" if v is None else str(v) for v in r] for r in rows]
    w = [max(len(r[i]) for r in cells) for i in range(len(head))]
    return "\n".join("  ".join(v.ljust(w[i]) for i, v in enumerate(r)).rstrip() for r in cells)


def duration(text: str) -> int:
    """'90' / '90m' / '2h' / '1d' → minutes."""
    m = re.fullmatch(r"\s*(\d+)\s*([mhd]?)\s*", text or "")
    if not m:
        raise argparse.ArgumentTypeError("a duration like 90m, 2h or 1d")
    return int(m.group(1)) * {"": 1, "m": 1, "h": 60, "d": 1440}[m.group(2)]


def hold_text(h: dict) -> str:
    if not h["held"]:
        return f"dispatch running · {h['running']} running · {h['pending']} waiting"
    s = f"DISPATCH HELD since {h['since']} by {h['by']}"
    if h.get("reason"):
        s += f" — {h['reason']}"
    if h.get("until"):
        s += f" (ends by itself at {h['until']})"
    return s + f"\n{h['running']} still running · {h['pending']} waiting"


# ---- commands -----------------------------------------------------------------------------------------------------
def cmd_login(args):
    url = args.url or os.environ.get("COMFYFARM_URL")
    if not url:
        raise Fail(USAGE, "give --url")
    if sys.stdin.isatty():
        token = getpass.getpass("API token (Settings → API tokens): ").strip()
    else:
        token = sys.stdin.readline().strip()
    if not token:
        raise Fail(USAGE, "no token given")
    me = Farm(url, token, args.timeout).call("GET", "/api/farm/me")     # check it before saving it
    save_config({"url": url.rstrip("/"), "token": token}, args.config)
    out(args, {"saved": args.config, "user": me["user"], "admin": me["admin"]},
        f"logged in to {url} as {me['user']}{' (admin)' if me['admin'] else ''} — saved in {args.config}")


def cmd_logout(args):
    try:
        os.remove(args.config)
        gone = True
    except FileNotFoundError:
        gone = False
    out(args, {"removed": gone}, f"removed {args.config}" if gone else "not logged in")


def cmd_whoami(args):
    me = connect(args).call("GET", "/api/farm/me")
    out(args, me, f"{me['user']}{' (admin)' if me['admin'] else ''} · groups: "
                  f"{'all' if me.get('all_groups') else ', '.join(g['name'] for g in me['groups']) or 'none'}")


def cmd_status(args):
    f = connect(args)
    h, mine = f.call("GET", "/api/farm/hold"), f.call("GET", "/api/farm/summary")
    doc = {"hold": h, "mine": mine}
    out(args, doc, hold_text(h) + f"\nmine: {mine['running']} running · {mine['queued']} queued")


def cmd_hold_status(args):
    h = connect(args).call("GET", "/api/farm/hold")
    out(args, h, hold_text(h))


def cmd_hold(args):
    f = connect(args)
    h = f.call("PUT", "/api/admin/hold", {"held": True, "reason": args.reason, "minutes": args.for_})
    if args.wait:
        deadline = time.monotonic() + args.wait_timeout
        while h["running"] and time.monotonic() < deadline:
            if not args.json:
                print(f"waiting for {h['running']} running job(s)…", file=sys.stderr)
            time.sleep(args.poll)
            h = f.call("GET", "/api/farm/hold")
        if h["running"]:
            raise Fail(FAILED, f"still {h['running']} job(s) running after {args.wait_timeout} s (dispatch stays held)", h)
    out(args, h, hold_text(h))


def cmd_release(args):
    h = connect(args).call("PUT", "/api/admin/hold", {"held": False})
    out(args, h, hold_text(h))


JOB_COLS = ["number", "prompt_id", "status", "owner", "group", "title"]


def job_row(j: dict) -> list:
    return [j.get("key"), j.get("prompt_id"), j.get("status"), j.get("owner"), j.get("group"), (j.get("title") or "")[:40]]


def cmd_jobs_list(args):
    f = connect(args)
    q = {"scope": "all" if args.all else "mine", "status": ",".join(args.status) if args.status else None, "q": args.search}
    if args.history:
        doc = f.call("GET", "/api/farm/history", query=q | {"limit": args.limit})
    else:
        doc = f.call("GET", "/api/farm/queue", query=q)
    out(args, doc, table([job_row(j) for j in doc["jobs"]], JOB_COLS) if doc["jobs"] else "no jobs")


def cmd_jobs_show(args):
    doc = connect(args).call("GET", f"/api/farm/jobs/{args.id}")
    out(args, doc, "\n".join(f"{k}: {v}" for k, v in doc.items() if k not in ("prompt", "extra_data")))


def per_job(args, verb: str, method: str = "POST", body=None):
    f, results, worst = connect(args), [], OK
    for pid in args.ids:
        try:
            results.append({"prompt_id": pid, "ok": True, "result": f.call(method, f"/api/farm/jobs/{pid}/{verb}", body)})
        except Fail as e:
            results.append({"prompt_id": pid, "ok": False, "error": str(e), "exit": e.code})
            worst = max(worst, e.code)
    out(args, {"results": results}, "\n".join(f"{r['prompt_id']}: " + (json.dumps(r["result"]) if r["ok"] else r["error"])
                                              for r in results))
    if worst:
        e = Fail(worst, "", {"results": results})
        e.printed = True
        raise e


def confirm(args, what: str):
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise Fail(USAGE, f"{what}: add --yes (not on a terminal, nobody to ask)")
    if input(f"{what}? [y/N] ").strip().lower() not in ("y", "yes"):
        raise Fail(FAILED, "not confirmed")


def cmd_jobs_pause(args):
    per_job(args, "pause")


def cmd_jobs_resume(args):
    per_job(args, "resume")


def cmd_jobs_retry(args):
    per_job(args, "retry")


def cmd_jobs_cancel(args):
    confirm(args, f"cancel {len(args.ids)} job(s)")
    per_job(args, "cancel")


def cmd_jobs_move(args):
    body = {"to": "top"} if args.top else {"to": "bottom"} if args.bottom else \
        {"before": args.before} if args.before else {"after": args.after}
    doc = connect(args).call("POST", f"/api/farm/jobs/{args.id}/move", body)
    out(args, doc, "moved")


def cmd_workers_list(args):
    doc = connect(args).call("GET", "/api/admin/workers")
    rows = [[w.get("id"), w.get("name"), w.get("kind"), w.get("group"), w.get("state"),
             "yes" if w.get("maintenance") else "", "" if w.get("enabled", True) else "disabled"] for w in doc]
    out(args, doc, table(rows, ["id", "name", "kind", "group", "state", "maintenance", ""]) if rows else "no workers")


# ---- phase 2: workers, profiles, rollouts, builds, node packs (admin) -----------------------------------------------
def pick(items: list[dict], ref: str, what: str, key: str = "name") -> dict:
    """An item by id (all digits) or by its exact name."""
    for it in items:
        if (ref.isdigit() and it.get("id") == int(ref)) or it.get(key) == ref:
            return it
    names = ", ".join(str(it.get(key)) for it in items[:20]) or "none"
    raise Fail(NOT_FOUND, f"no {what} {ref!r} (there are: {names})")


def wait_until(args, poll, done, describe, what: str):
    """Poll `poll()` until `done(doc)`; print each new description on stderr (not with --json). Returns the last doc."""
    deadline, last, doc = time.monotonic() + args.wait_timeout, None, poll()
    while not done(doc):
        line = describe(doc)
        if line != last and not args.json:
            print(line, file=sys.stderr)
            last = line
        if time.monotonic() >= deadline:
            raise Fail(FAILED, f"{what} still not finished after {args.wait_timeout:.0f} s — it goes on; look again with "
                               f"`show`", doc)
        time.sleep(args.poll)
        doc = poll()
    return doc


def cmd_workers_show(args):
    w = pick(connect(args).call("GET", "/api/admin/workers"), args.worker, "worker")
    out(args, w, "\n".join(f"{k}: {v}" for k, v in w.items() if k not in ("capabilities", "memory", "reset")))


def worker_patch(args, body: dict, done: str):
    f = connect(args)
    ws = f.call("GET", "/api/admin/workers")
    results, worst = [], OK
    for ref in args.workers:
        try:
            w = pick(ws, ref, "worker")
            results.append({"worker": w["name"], "ok": True, "result": f.call("PATCH", f"/api/admin/workers/{w['id']}", body)})
        except Fail as e:
            results.append({"worker": ref, "ok": False, "error": str(e), "exit": e.code})
            worst = max(worst, e.code)
    out(args, {"results": results}, "\n".join(f"{r['worker']}: " + (done if r["ok"] else r["error"]) for r in results))
    if worst:
        e = Fail(worst, "", {"results": results})
        e.printed = True
        raise e


def cmd_workers_maintenance(args):
    on = args.state == "on"
    worker_patch(args, {"maintenance": on}, "maintenance on — no new jobs; a running one finishes" if on else "maintenance off")


def cmd_workers_enable(args):
    worker_patch(args, {"enabled": True}, "enabled")


def cmd_workers_disable(args):
    worker_patch(args, {"enabled": False}, "disabled — gets no jobs")


def cmd_workers_reset(args):
    """Force reset (D-53): its jobs end now and its ComfyUI restarts. Shows what it would do first."""
    f = connect(args)
    w = pick(f.call("GET", "/api/admin/workers"), args.worker, "worker")
    preview = f.call("GET", f"/api/admin/workers/{w['id']}/force-reset")
    if not args.json:
        print(json.dumps(preview, ensure_ascii=False, indent=1), file=sys.stderr)
    confirm(args, f"force reset {w['name']} (its jobs are cancelled, ComfyUI restarts)")
    doc = f.call("POST", f"/api/admin/workers/{w['id']}/force-reset")
    out(args, {"preview": preview, "result": doc}, json.dumps(doc, ensure_ascii=False))


def cmd_profiles_list(args):
    doc = connect(args).call("GET", "/api/admin/profiles")
    rows = [[p["id"], p["name"], p.get("core"), p.get("venv"), p.get("nodes") and ",".join(p["nodes"]), p.get("assigned")]
            for p in doc]
    out(args, doc, table(rows, ["id", "name", "core", "venv", "nodes", "assigned"]) if rows else "no profiles")


ROLLOUT_ACTIVE = ("running", "aborting")


def rollout_line(r: dict) -> str:
    steps = " · ".join(f"{s['worker']} {s['status']}" for s in r.get("steps", []))
    return f"rollout #{r['id']} → {r['profile']}: {r['status']}" + (f" — {steps}" if steps else "") + \
        (f" — {r['error']}" if r.get("error") else "")


def rollout_result(args, f, rid: int, ok=("succeeded",)):
    """With --wait: until the rollout ends. Ending as `ok` = 0 (succeeded; for abort, aborted); anything else = 1."""
    doc = f.call("GET", f"/api/admin/rollouts/{rid}")
    if getattr(args, "wait", False):
        doc = wait_until(args, lambda: f.call("GET", f"/api/admin/rollouts/{rid}"),
                         lambda r: r["status"] not in ROLLOUT_ACTIVE, rollout_line, f"rollout #{rid}")
    out(args, doc, rollout_line(doc))
    if getattr(args, "wait", False) and doc["status"] not in ok:
        e = Fail(FAILED, f"rollout #{rid} {doc['status']}" + (" — `rollouts resume` or `rollouts abort`"
                                                              if doc["status"] == "interrupted" else ""), doc)
        e.printed = True
        raise e


def cmd_rollouts_list(args):
    doc = connect(args).call("GET", "/api/admin/rollouts", query={"limit": args.limit})
    out(args, doc, "\n".join(rollout_line(r) for r in doc) or "no rollouts")


def cmd_rollouts_show(args):
    rollout_result(args, connect(args), args.id)


def cmd_rollouts_start(args):
    f = connect(args)
    prof = pick(f.call("GET", "/api/admin/profiles"), args.profile, "profile")
    body = {"profile_id": prof["id"], "window": args.window, "options": {}}
    if args.group:
        body["group_id"] = pick(f.call("GET", "/api/admin/groups"), args.group, "group")["id"]
    if args.worker:
        ws = f.call("GET", "/api/admin/workers")
        body["worker_ids"] = [pick(ws, ref, "worker")["id"] for ref in args.worker]
    if not args.group and not args.worker:
        raise Fail(USAGE, "name the workers: --worker NAME (repeatable) or --group NAME")
    for k in ("smoke", "restart"):
        if getattr(args, k) is not None:
            body["options"][k] = getattr(args, k)
    for k in ("drain_timeout", "start_timeout", "verify_timeout"):
        if getattr(args, k) is not None:
            body["options"][k] = getattr(args, k)
    confirm(args, f"roll {prof['name']} out (each worker is drained and restarted, one window at a time)")
    r = f.call("POST", "/api/admin/rollouts", body)
    for w in r.get("warnings", []):
        print(f"warning: {w}", file=sys.stderr)
    rollout_result(args, f, r["id"])


def cmd_rollouts_abort(args):
    confirm(args, f"abort rollout #{args.id}")
    f = connect(args)
    f.call("POST", f"/api/admin/rollouts/{args.id}/abort")
    rollout_result(args, f, args.id, ok=("aborted",))


def cmd_rollouts_resume(args):
    f = connect(args)
    f.call("POST", f"/api/admin/rollouts/{args.id}/resume")
    rollout_result(args, f, args.id)


def build_line(b: dict) -> str:
    what = b.get("recipe") or b.get("kind") or ""
    return f"{b.get('id')} {what}: {b.get('state')}" + (f" — {b['template']}" if b.get("template") else "") + \
        (f" — {b['error']}" if b.get("error") else "")


def build_result(args, f, rid: str, wait: bool):
    poll = lambda: f.call("GET", f"/api/admin/builds/{rid}")
    doc = wait_until(args, poll, lambda b: b.get("state") in ("done", "failed"), build_line, f"build {rid}") if wait else poll()
    out(args, doc, build_line(doc) + (("\n" + doc["log"][-2000:]) if args.log and doc.get("log") else ""))
    if wait and doc.get("state") == "failed":
        e = Fail(FAILED, f"build {rid} failed: {doc.get('error')}", doc)
        e.printed = True
        raise e
    return doc


def cmd_builds_recipes(args):
    doc = connect(args).call("GET", "/api/admin/builder")
    about = doc.get("about") or {}
    out(args, doc, ("builder up" if doc.get("up") else "BUILDER DOWN") + "\n" +
        "\n".join(f"{r}  {about.get(r, '')}".rstrip() for r in doc.get("recipes", [])))


def cmd_builds_list(args):
    doc = connect(args).call("GET", "/api/admin/builds")
    out(args, doc, "\n".join(build_line(b) for b in doc) or "no builds")


def cmd_builds_show(args):
    build_result(args, connect(args), args.id, args.wait)


def cmd_builds_start(args):
    f = connect(args)
    roles = [r.strip() for r in args.roles.split(",") if r.strip()]
    r = f.call("POST", "/api/admin/builds", {"recipe": args.recipe, "roles": roles, "packs": not args.no_packs})
    if not args.json:
        print(f"build {r['id']} requested ({r['packs']} node pack(s))", file=sys.stderr)
    build_result(args, f, r["id"], args.wait)


def resolve_commit(args, f, repo: str, ref: str) -> str:
    """A branch or tag → the commit it names now, asked of the builder (it has the network)."""
    rid = f.call("POST", "/api/admin/node-packs/resolve", {"repo": repo, "ref": ref})["id"]
    old = args.wait_timeout
    args.wait_timeout = min(old, args.lookup_timeout)
    try:
        st = wait_until(args, lambda: f.call("GET", f"/api/admin/builds/{rid}"), lambda b: b.get("state") in ("done", "failed"),
                        lambda b: f"looking up {ref or 'the default branch'} of {repo}…", "the lookup")
    finally:
        args.wait_timeout = old
    if st.get("state") != "done":
        raise Fail(FAILED, f"could not look up {ref or 'the default branch'} of {repo}: {st.get('error')}", st)
    return st["commit"]


def cmd_packs_list(args):
    doc = connect(args).call("GET", "/api/admin/node-packs")
    rows = [[p["id"], p["name"], p.get("archive_name") or p.get("repo"), (p.get("commit") or "")[:10], p.get("ref") or "",
             "" if p.get("ui") else "gpu only"] for p in doc]
    out(args, doc, table(rows, ["id", "name", "source", "commit", "ref", ""]) if rows else "no node packs")


def cmd_packs_add(args):
    f = connect(args)
    repo = args.repo.strip().rstrip("/")
    name = args.name or repo.split("/")[-1].removesuffix(".git")
    commit = resolve_commit(args, f, repo, args.ref or "")
    doc = f.call("POST", "/api/admin/node-packs", {
        "name": name, "repo": repo, "commit": commit, "ref": args.ref or None, "ui": not args.gpu_only,
        "requirements_file": args.requirements_file, "constraints": args.constraint or [], "note": args.note})
    out(args, doc | {"name": name, "commit": commit}, f"{name} added at {commit[:10]} — `builds start RECIPE` to build it")


def cmd_packs_update(args):
    f = connect(args)
    p = pick(f.call("GET", "/api/admin/node-packs"), args.pack, "node pack")
    if p.get("archive"):
        raise Fail(USAGE, f"{p['name']} came from an archive: its new version is another archive (the dashboard uploads it)")
    ref = p.get("ref") or ""
    commit = resolve_commit(args, f, p["repo"], ref)
    if commit == p["commit"]:
        return out(args, {"name": p["name"], "commit": commit, "changed": False},
                   f"{p['name']}: already the newest commit of {ref or 'the default branch'}")
    f.call("PATCH", f"/api/admin/node-packs/{p['id']}", {"commit": commit})
    out(args, {"name": p["name"], "from": p["commit"], "commit": commit, "changed": True},
        f"{p['name']}: {p['commit'][:10]} → {commit[:10]} — build to use it")


def cmd_packs_remove(args):
    f = connect(args)
    p = pick(f.call("GET", "/api/admin/node-packs"), args.pack, "node pack")
    confirm(args, f"remove the node pack {p['name']} (later builds leave it out)")
    out(args, f.call("DELETE", f"/api/admin/node-packs/{p['id']}"), f"{p['name']} removed")


def cmd_commands(args):
    doc = {"commands": catalog(parser()), "exit_codes": {"0": "ok", "1": "refused or failed", "2": "usage",
                                                         "3": "not authenticated or not allowed", "4": "conflict",
                                                         "5": "not found", "6": "farm unavailable"},
           "env": {"COMFYFARM_URL": "the farm's address", "COMFYFARM_TOKEN": "an API token"}}
    if args.json:
        out(args, doc)
    else:
        for c in doc["commands"]:
            print(f"{c['command']:<20} {c['help']}")


# ---- parser -------------------------------------------------------------------------------------------------------
COMMON = ("url", "json", "compact", "timeout", "config")


def common_options(sub: bool) -> argparse.ArgumentParser:
    """The same options before or after the command (`--json status` and `status --json`). On the commands they
    default to SUPPRESS: a subparser's defaults would otherwise overwrite what was given before the command."""
    d = (lambda v: argparse.SUPPRESS) if sub else (lambda v: v)
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("--url", default=d(None), help="the farm's address (default: COMFYFARM_URL, then the saved login)")
    c.add_argument("--json", action="store_true", default=d(False), help="print the API's answer as JSON")
    c.add_argument("--compact", action="store_true", default=d(False), help="with --json: one line")
    c.add_argument("--timeout", type=float, default=d(TIMEOUT), help="seconds per request")
    c.add_argument("--config", default=d(CONFIG), help=argparse.SUPPRESS)
    return c


def parser() -> argparse.ArgumentParser:
    common = common_options(sub=True)
    p = argparse.ArgumentParser(prog="comfyfarm-ctl", description=__doc__.split("\n\n")[0], parents=[common_options(sub=False)])
    sub = p.add_subparsers(dest="cmd", metavar="command")

    def add(name, fn, help_, parent=sub):
        sp = parent.add_parser(name, help=help_, description=help_, parents=[common])
        sp.set_defaults(fn=fn)
        return sp

    add("login", cmd_login, "save the farm's address and an API token (read from stdin) in ~/.config/comfyfarm/ctl.json")
    add("logout", cmd_logout, "forget the saved login")
    add("whoami", cmd_whoami, "the user the token acts as")
    add("status", cmd_status, "dispatch hold, the farm's totals and your own queue")
    sp = add("hold", cmd_hold, "hold dispatch: no job goes to a worker; running jobs finish, new ones queue (admin)")
    sp.add_argument("--reason", help="shown to every user in the dashboard's banner")
    sp.add_argument("--for", dest="for_", type=duration, metavar="DURATION", help="end the hold by itself after 90m, 2h, 1d…")
    sp.add_argument("--wait", action="store_true", help="then wait until no job is running (a drained farm)")
    sp.add_argument("--wait-timeout", type=float, default=3600, help="seconds --wait waits at most")
    sp.add_argument("--poll", type=float, default=5, help=argparse.SUPPRESS)
    add("release", cmd_release, "end the hold: dispatch starts again (admin)")
    add("hold-status", cmd_hold_status, "is dispatch held, by whom, since when")

    jobs = sub.add_parser("jobs", help="list and act on jobs", parents=[common]).add_subparsers(dest="jobs_cmd", metavar="action")
    sp = add("list", cmd_jobs_list, "unfinished jobs (yours; --all = everyone's, admin)", jobs)
    sp.add_argument("--all", action="store_true", help="everyone's jobs (admin)")
    sp.add_argument("--status", action="append", help="pending, paused, dispatching, running, cancelling — or with "
                                                      "--history completed, failed, cancelled (repeatable)")
    sp.add_argument("--history", action="store_true", help="finished jobs instead")
    sp.add_argument("--search", help="text in the title, the owner or the prompt id")
    sp.add_argument("--limit", type=int, default=50, help="with --history: how many")
    sp = add("show", cmd_jobs_show, "one of your jobs in full", jobs)
    sp.add_argument("id", help="prompt id")
    for name, fn, help_ in (("pause", cmd_jobs_pause, "pending → paused"), ("resume", cmd_jobs_resume, "paused → pending"),
                            ("retry", cmd_jobs_retry, "a finished job again, as a new job"),
                            ("cancel", cmd_jobs_cancel, "cancel (a running job is interrupted)")):
        sp = add(name, fn, help_, jobs)
        sp.add_argument("ids", nargs="+", metavar="id", help="prompt id(s)")
        if name == "cancel":
            sp.add_argument("--yes", action="store_true", help="do not ask")
    sp = add("move", cmd_jobs_move, "change a queued job's place (admin)", jobs)
    sp.add_argument("id", help="prompt id")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--top", action="store_true")
    g.add_argument("--bottom", action="store_true")
    g.add_argument("--before", metavar="ID")
    g.add_argument("--after", metavar="ID")

    def waiting(sp, default=False, what="it ends"):
        if not default:
            sp.add_argument("--wait", action="store_true", help=f"wait until {what}; the exit code says how it went")
        sp.add_argument("--wait-timeout", type=float, default=6 * 3600, help="seconds --wait waits at most")
        sp.add_argument("--poll", type=float, default=5, help=argparse.SUPPRESS)

    workers = sub.add_parser("workers", help="workers (admin)", parents=[common]).add_subparsers(dest="workers_cmd", metavar="action")
    add("list", cmd_workers_list, "every worker with its state", workers)
    sp = add("show", cmd_workers_show, "one worker in full", workers)
    sp.add_argument("worker", help="name or id")
    sp = add("maintenance", cmd_workers_maintenance, "maintenance on: no new jobs, a running one finishes · off: back to work",
             workers)
    sp.add_argument("state", choices=["on", "off"])
    sp.add_argument("workers", nargs="+", metavar="worker", help="name(s) or id(s)")
    for name, fn, help_ in (("enable", cmd_workers_enable, "enable (gets jobs again)"),
                            ("disable", cmd_workers_disable, "disable (gets no jobs)")):
        sp = add(name, fn, help_, workers)
        sp.add_argument("workers", nargs="+", metavar="worker", help="name(s) or id(s)")
    sp = add("reset", cmd_workers_reset, "force reset a worker too busy to answer: its jobs end now, ComfyUI restarts", workers)
    sp.add_argument("worker", help="name or id")
    sp.add_argument("--yes", action="store_true", help="do not ask")

    profiles = sub.add_parser("profiles", help="rollout profiles (admin)", parents=[common]).add_subparsers(dest="profiles_cmd", metavar="action")
    add("list", cmd_profiles_list, "every profile (core, venv, node set)", profiles)

    rollouts = sub.add_parser("rollouts", help="rolling upgrades (admin)", parents=[common]).add_subparsers(dest="rollouts_cmd", metavar="action")
    sp = add("list", cmd_rollouts_list, "recent rollouts with their steps", rollouts)
    sp.add_argument("--limit", type=int, default=10)
    sp = add("show", cmd_rollouts_show, "one rollout", rollouts)
    sp.add_argument("id", type=int)
    waiting(sp, what="the rollout ends")
    sp = add("start", cmd_rollouts_start, "roll a profile out to workers: drained, restarted, verified, reverted on failure", rollouts)
    sp.add_argument("--profile", required=True, help="profile name or id")
    sp.add_argument("--worker", action="append", help="worker name or id (repeatable)")
    sp.add_argument("--group", help="every enabled worker of this group")
    sp.add_argument("--window", type=int, default=1, help="workers at a time (default 1)")
    sp.add_argument("--smoke", action=argparse.BooleanOptionalAction, default=None, help="run the smoke test after the start")
    sp.add_argument("--restart", action=argparse.BooleanOptionalAction, default=None,
                    help="restart even a worker that already runs the profile")
    for k in ("drain", "start", "verify"):
        sp.add_argument(f"--{k}-timeout", type=float, default=None, help=f"seconds for the {k} phase")
    sp.add_argument("--yes", action="store_true", help="do not ask")
    waiting(sp, what="the rollout ends")
    for name, fn, help_ in (("abort", cmd_rollouts_abort, "stop it: no new steps, a draining worker goes back to work"),
                            ("resume", cmd_rollouts_resume, "go on with an interrupted rollout")):
        sp = add(name, fn, help_, rollouts)
        sp.add_argument("id", type=int)
        if name == "abort":
            sp.add_argument("--yes", action="store_true", help="do not ask")
        waiting(sp, what="the rollout ends")

    builds = sub.add_parser("builds", help="artifact builds on the control plane (admin)", parents=[common]).add_subparsers(dest="builds_cmd", metavar="action")
    add("recipes", cmd_builds_recipes, "is the builder up, which recipes it has", builds)
    add("list", cmd_builds_list, "recent builds", builds)
    sp = add("show", cmd_builds_show, "one build (--log: its log's tail)", builds)
    sp.add_argument("id")
    sp.add_argument("--log", action="store_true", help="print the tail of its log")
    waiting(sp, what="the build ends")
    sp = add("start", cmd_builds_start, "build a recipe (with the node packs) into the artifact store", builds)
    sp.add_argument("recipe")
    sp.add_argument("--roles", default="gpu,ui", help="gpu, ui or gpu,ui (default)")
    sp.add_argument("--no-packs", action="store_true", help="without the dashboard's node packs")
    sp.add_argument("--log", action="store_true", help="print the tail of its log")
    waiting(sp, what="the build ends")

    packs = sub.add_parser("packs", help="node packs the builds take (admin)", parents=[common]).add_subparsers(dest="packs_cmd", metavar="action")
    add("list", cmd_packs_list, "every node pack with its pinned commit", packs)
    sp = add("add", cmd_packs_add, "add a node pack from git, pinned to the commit a branch or tag names now", packs)
    sp.add_argument("repo", help="https git URL")
    sp.add_argument("--ref", help="branch, tag or commit (default: the default branch)")
    sp.add_argument("--name", help="its directory under custom_nodes (default: from the URL)")
    sp.add_argument("--gpu-only", action="store_true", help="not on the UI worker (a pack that imports only with a GPU)")
    sp.add_argument("--requirements-file", help="default: requirements.txt, else pyproject.toml")
    sp.add_argument("--constraint", action="append", help="hold a dependency back, e.g. 'kornia<0.8' (repeatable)")
    sp.add_argument("--note")
    sp = add("update", cmd_packs_update, "move a pack to the newest commit of its branch or tag", packs)
    sp.add_argument("pack", help="name or id")
    sp = add("remove", cmd_packs_remove, "remove a node pack (later builds leave it out)", packs)
    sp.add_argument("pack", help="name or id")
    sp.add_argument("--yes", action="store_true", help="do not ask")
    for sp in (packs.choices["add"], packs.choices["update"]):
        sp.add_argument("--lookup-timeout", type=float, default=120, help="seconds the builder may take to look the commit up")
        sp.add_argument("--wait-timeout", type=float, default=120, help=argparse.SUPPRESS)
        sp.add_argument("--poll", type=float, default=1, help=argparse.SUPPRESS)
    add("commands", cmd_commands, "every command and its arguments (--json for programs)")
    return p


def catalog(p: argparse.ArgumentParser, prefix: str = "") -> list[dict]:
    """Commands with their arguments, read from the parser itself so it cannot drift from what is accepted."""
    found = []
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            for choice in a._choices_actions:
                sp = a.choices[choice.dest]
                name = f"{prefix}{choice.dest}"
                if sp.get_default("fn") is not None:
                    args = [{"name": x.dest if not x.option_strings else x.option_strings[0], "help": x.help,
                             "required": bool(x.required) if x.option_strings else x.nargs not in ("?", "*"),
                             "flag": x.nargs == 0} for x in sp._actions
                            if x.help != argparse.SUPPRESS and not isinstance(x, argparse._HelpAction)
                            and x.dest not in COMMON]
                    found.append({"command": name, "help": choice.help, "arguments": args})
                found += catalog(sp, name + " ")
    return found


def main(argv: list[str] | None = None) -> int:
    p = parser()
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return USAGE if e.code else OK
    if getattr(args, "fn", None) is None:
        p.print_help(sys.stderr)
        return USAGE
    try:
        args.fn(args)
        return OK
    except Fail as e:
        if str(e):
            print(f"comfyfarm-ctl: {e}", file=sys.stderr)
        if getattr(args, "json", False) and not getattr(e, "printed", False):   # a program reads stdout, not stderr
            print(json.dumps({"error": str(e), "exit": e.code, "answer": e.body}, ensure_ascii=False))
        return e.code
    except KeyboardInterrupt:
        return FAILED


if __name__ == "__main__":
    sys.exit(main())
