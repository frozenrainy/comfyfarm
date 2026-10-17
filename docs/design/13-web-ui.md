# 13 — Web UI

The dashboard is served by the router at `/comfyfarm/dashboard/` (through the gateway like everything else). One HTML
file, one CSS file and a few ES modules — no framework, no build step, no package registry needed to patch it (D-32).
Pages are hash routes; each page mounts into the shell and exposes `refresh()`. Pages showing live state poll (2–3 s,
15 s while the tab is hidden); lists one reads or searches (history, statistics, settings) refresh on demand.

| Page | Who | Content |
|---|---|---|
| Dashboard `#/` | everybody | own unfinished jobs with their **place in line** (within their group, counting everybody's jobs) and a *blocked* mark when no group the owner may use can run the job; cards of the groups the user may use; own usage (24 h / 7 days / 30 days); last 10 finished jobs |
| Queue `#/queue` | everybody | own unfinished jobs (admins: everybody's), search (id, note; admins also owner), status and group filters; pause/resume/cancel; admins reorder (⤒ ↑ ↓ ⤓) |
| History `#/history` | everybody | finished jobs, newest first, 50 per page; search (id, note, error message; admins also owner), status/group/date filters; retry; notes |
| Settings `#/settings` | everybody | theme (light — the default —, dark, auto), password change (local accounts; ends all sessions), groups one may use |
| System `#/admin/…` | admins | **Workers** (add, edit name/URL, group, maintenance, disable, delete, notes, warnings) · **Groups** (create, rename, requirements, refresh node list, delete when empty) · **Users** (create, admin role, groups, new password, disable, delete) · **Rollout** (instances with badge, profiles, start/abort/resume rollouts with live steps, manual-restart call-out, audit log; [guide](../guide/rollouts.md)) · **Statistics** |

Statistics (admin page; the dashboard shows the same numbers for one user): a period of **at most 30 days** — presets
24 h / 7 days / 30 days or two calendar dates in the browser's time zone. Totals, a daily bar chart (completed / failed
/ cancelled), and tables by group, user and worker. Jobs count in the period they finished; *GPU time* is worker
occupancy (dispatch → finish, model loading included); *avg wait* is submit → dispatch, *avg run* start → finish of
completed jobs; *busy* is the share of the period a worker held any job.

Rules the implementation keeps:

- **Rows are updated in place by key**, gone rows removed first. Re-rendering a table wiped a note being typed; moving a
  row (when the rows above it were removed last) took the keyboard focus away (both seen in the browser E2E).
- **An unreachable router is shown**, with the time of the last good data, never as an empty (= idle-looking) farm.
- The server enforces every rule the UI shows (admin pages, reorder, groups, 30-day limit); hidden buttons are
  convenience, not security.
- Text is escaped by construction (DOM nodes and `textContent`, no HTML strings with data).
- Korean and other CJK text wraps at word boundaries (`word-break: keep-all`); tables scroll sideways instead of
  breaking words, and have no sticky headers inside the scroll container.
- Dashboard files are served `Cache-Control: no-cache` so an upgrade never mixes cached old modules with new ones.

E2E: `spikes/e2e_dashboard.js` against `spikes/e2e_stack.py` (login redirect, reorder, pause, note while polling,
outage banner, user administration, statistics limits, history search, server-side theme, a non-admin's view,
password change).
