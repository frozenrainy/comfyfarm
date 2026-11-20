# Command line: `comfyfarm-ctl`

`comfyfarm-ctl` runs a farm from a terminal or from a program (a script, an AI agent) through the farm's API. It acts
as the user an **API token** belongs to, with the same permissions and the same audit log as the dashboard. Design and
reasons: [design 19](../design/19-cli-and-api-tokens.md).

It needs only Python 3.10+. It comes with the package (`comfyfarm-ctl`), or copy `src/comfyfarm/ctl.py` anywhere and run
`python3 ctl.py`.

(The `comfyfarm` command is a different tool: it runs on the control-plane host against the database — install, backup,
repair.)

## A token

Dashboard → your name (top right) → **API tokens** → **New token**. Pick a scope:

| Scope | Can |
|---|---|
| Read only | look (GET), with your rights |
| Act as me (no administration) | everything you can do, except administration |
| Act as me, administration included | everything — administrators only |

The token is shown once. It never has more rights than you have *now*: if an administrator disables your account or
takes away your admin role, it follows within 30 seconds. Changing your password does **not** end your tokens; revoke
them on the same page. Administrators see and revoke everyone's under System → Users.

## Connecting

```sh
comfyfarm-ctl login --url https://farm.example.org     # paste the token (read from stdin, saved 0600)
comfyfarm-ctl whoami
```

or, for programs and CI, the environment: `COMFYFARM_URL` and `COMFYFARM_TOKEN`. The token is never taken as an
argument — it would end up in shell history and process lists.

## Holding the whole farm

```sh
comfyfarm-ctl hold --reason "driver update" --for 2h --wait   # admin: stop handing out jobs; wait until none runs
comfyfarm-ctl status                                          # who held it, since when, what is still running
comfyfarm-ctl release                                         # dispatch starts again
```

While dispatch is held, jobs already on a worker run to the end and new jobs wait in the queue. Jobs that people paused
stay paused, before and after. Every user sees a banner in the dashboard. `--for` makes the hold end by itself, so a
forgotten hold cannot keep the farm idle. Monitoring: `comfyfarm_dispatch_held_timestamp_seconds`
([06](../design/06-health-and-monitoring.md)).

## Jobs and workers

```sh
comfyfarm-ctl jobs list                       # yours; --all (admin) everyone's; --history finished ones
comfyfarm-ctl jobs pause  <prompt_id>…        # pending → paused
comfyfarm-ctl jobs resume <prompt_id>…
comfyfarm-ctl jobs cancel <prompt_id>… --yes
comfyfarm-ctl jobs retry  <prompt_id>
comfyfarm-ctl jobs move   <prompt_id> --top   # admin: --bottom, --before ID, --after ID
```

## Workers, rollouts, builds, node packs (admin)

```sh
comfyfarm-ctl workers list
comfyfarm-ctl workers maintenance on gpu-1 gpu-2   # no new jobs; a running one finishes · off: back to work
comfyfarm-ctl workers disable gpu-3                # enable: back
comfyfarm-ctl workers reset gpu-1                  # too busy to answer: its jobs end now, ComfyUI restarts (asks first)

comfyfarm-ctl packs add https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite --ref main
comfyfarm-ctl packs update ComfyUI-VideoHelperSuite   # to the newest commit of its branch or tag
comfyfarm-ctl builds recipes                          # is the builder up, which recipes
comfyfarm-ctl builds start rocm-gfx1201 --wait        # exit 1 if the build failed
comfyfarm-ctl profiles list
comfyfarm-ctl rollouts start --profile farm-gpu-27 --group gpu --wait --yes   # exit 1 unless it succeeded
comfyfarm-ctl rollouts abort 12 --wait
```

Workers, groups, profiles and node packs are named by name or id. Profiles are still made in the dashboard
(System → Rollout → Profiles → From a recipe).

## For programs and agents

- `--json` prints the API's answer as JSON (`--compact`: one line). It goes before or after the command. Errors are
  JSON on stdout too: `{"error": …, "exit": …, "answer": …}`.
- `comfyfarm-ctl commands --json` lists every command and its arguments.
- Exit codes:

  | Code | Meaning |
  |---|---|
  | 0 | ok |
  | 1 | refused or failed |
  | 2 | usage |
  | 3 | not authenticated or not allowed |
  | 4 | conflict, e.g. the job is not pending |
  | 5 | not found |
  | 6 | farm unavailable |

- `jobs cancel`, `workers reset`, `rollouts start`/`abort` and `packs remove` ask on a terminal; elsewhere they need
  `--yes`.
- `--wait` follows rollouts, builds and `hold` to the end; the exit code says how they ended. `--wait-timeout` bounds
  it (the operation itself goes on).
- An agent should get a token of its own, so the audit log tells its actions apart from yours (System → Rollout → Audit
  log shows the token's name on each entry). Give it **Read only** when it only watches, and **Act as me** to operate.

Coming next: users, groups, models and uploads, settings, profiles from a recipe.
