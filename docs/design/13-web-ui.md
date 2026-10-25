# 13 — Web UI

The dashboard is served by the router at `/comfyfarm/dashboard/` (through the gateway like everything else). One HTML
file, one CSS file and a few ES modules — no framework, no build step, no package registry needed to patch it (D-32).
Pages are hash routes; each page mounts into the shell and exposes `refresh()`. Pages showing live state poll (2–3 s,
15 s while the tab is hidden); lists one reads or searches (history, statistics, settings) refresh on demand.

| Page | Who | Content |
|---|---|---|
| Workflow `#/comfyui` | everybody | the stock ComfyUI (served through the gateway, same origin) in an iframe under the ComfyFarm header. Created once and only hidden on other tabs, so an unsaved workflow, running previews and its WebSocket survive tab switches. `ComfyUI ↗` opens it bare in a new tab |
| Dashboard `#/` | everybody | own unfinished jobs with their **place in line** (within their group, counting everybody's jobs) and a *blocked* mark when no group the owner may use can run the job; cards of the groups the user may use; own usage (24 h / 7 days / 30 days); last 10 finished jobs |
| Jobs `#/jobs` | everybody | one table (dev.21; it replaced *Queue* and *History*, whose addresses redirect): own jobs (admins: everybody's), the unfinished ones on top and live, the finished ones below, newest first, 50 a page and read again when a job finishes; search (title, id, note, error message; admins also owner), status/group/date filters; pause/resume/cancel, retry, notes; admins reorder (⤒ ↑ ↓ ⤓). Columns are of fixed width — a long title is cut with …; the error is a ⚠ next to the status that opens the whole message; the first file is a thumbnail and `+N` opens all of them; a running job's bar is weighed by sampler steps with the time left under it, or "preparing…" / "no word for 45 s" ([05](05-events.md)) |
| Settings `#/settings` | everybody | theme (auto — the default, follows the system —, light, dark), language (auto — the browser's —, English, 한국어), password change (local accounts; ends all sessions), groups one may use |
| System `#/admin/…` | admins | **Workers** (add, edit name/URL, group, maintenance, disable, delete, notes, warnings) · **Groups** (create, rename, requirements, refresh node list, delete when empty) · **Users** (create, admin role, groups, new password, disable, delete) · **Rollout** (instances with badge, profiles, start/abort/resume rollouts with live steps, manual-restart call-out, audit log; [guide](../guide/rollouts.md)) · **Statistics** |

Statistics (admin page; the dashboard shows the same numbers for one user): a period of **at most 30 days** — presets
24 h / 7 days / 30 days or two calendar dates in the browser's time zone. Totals, a daily bar chart (completed / failed
/ cancelled), and tables by group, user and worker. Jobs count in the period they finished; *GPU time* is worker
occupancy (dispatch → finish, model loading included); *avg wait* is submit → dispatch, *avg run* start → finish of
completed jobs; *busy* is the share of the period a worker held any job.

Header: a **queue badge** with the user's unfinished jobs (polled every 5 s; the ComfyUI extension posts a same-origin
message to the shell after each submit, so it updates at once — the shell accepts messages only from its own frame and
origin).

**Login** is the gateway's page in the dashboard's look (theme from the browser's last choice) and lands on the
dashboard. *System → Settings → login user list* turns the user field into a drop-down of active local accounts
(convenient on a small trusted network; anyone who can open the login page then sees the names — off by default).
Inside the Workflow frame an expired session would show the login page; it takes over the whole window instead.

**Group picker in ComfyUI.** The extension adds a `Group: …` button to ComfyUI's action bar next to Run (official
`actionBarButtons` API). It lists the groups the user may use plus *Any of my groups* (no group: the job runs on any
group the user may use; the dashboard's history says *(any)*, and the node list is `COMFYFARM_DEFAULT_GROUP`'s when
set); the choice is kept in the browser
(localStorage) and sent with every submit and node-list request. Changing it reloads ComfyUI (node definitions are per
group, D-20; the open workflow is restored). A remembered group the user may no longer use is dropped in the
extension's `init` hook, which runs before the node definitions are fetched.

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
