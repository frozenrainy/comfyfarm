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


def cmd_commands(args):
    doc = {"commands": catalog(parser()), "exit_codes": {"0": "ok", "1": "refused or failed", "2": "usage",
                                                         "3": "not authenticated or not allowed", "4": "conflict",
                                                         "5": "not found", "6": "farm unavailable"},
           "env": {"COMFYFARM_URL": "the farm's address", "COMFYFARM_TOKEN": "an API token"}}
    if args.json:
        out(args, doc)
    else:
        for c in doc["commands"]:
            print(f"{c['command']:<14} {c['help']}")


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

    workers = sub.add_parser("workers", help="workers (admin)", parents=[common]).add_subparsers(dest="workers_cmd", metavar="action")
    add("list", cmd_workers_list, "every worker with its state", workers)
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
