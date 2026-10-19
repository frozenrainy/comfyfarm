# Adding custom nodes

A custom node reaches the workers the same way ComfyUI itself does: as published artifacts, tied together by a
profile and rolled out (rollouts.md). Nothing is installed on a worker by hand, and ComfyUI-Manager is not used —
every worker of a group must run exactly the same nodes. Worked example at the end: ComfyUI-VideoHelperSuite.

## 1. Look at the node first

Pin a commit (or a release) and read four things:

- **Python requirements** — they go into a venv (§3).
- **A `web/` directory** — frontend JavaScript. Browsers load it from the UI worker, so the UI worker needs the node too.
- **Server routes** (`PromptServer.instance.routes`, `add_route`) — the gateway sends every request it does not know to
  the UI worker ([design 08](../design/08-gateway-and-identity.md)). Routes that read files work for `input`,
  `output` and `user` (shared storage); a file only the executing worker has (`temp`) is reachable through `/view`
  only (§5).
- **Settings in environment variables, and anything that touches server paths** — §6 and §7.

## 2. Publish node sets

A node set is a directory of node directories. Make two when the node has a `web/` directory or routes — one for the
GPU workers, one for the UI worker:

```sh
git -C ComfyUI-SomeNode archive --prefix=ComfyUI-SomeNode/ <commit> | tar -x -C /tmp/gpu-set   # no .git in a set
cp -r comfyfarm/worker_ext/comfyfarm_worker /tmp/gpu-set/
cp -r /tmp/gpu-set /tmp/ui-set && cp -r comfyfarm/ui_ext/comfyfarm_ui /tmp/ui-set/
python3 publish.py nodes <ART> farm-gpu-2 --from /tmp/gpu-set
python3 publish.py nodes <ART> farm-ui-2 --from /tmp/ui-set
```

Names are never reused: the next change is `farm-gpu-3`.

## 3. A venv with the node's requirements

Venvs are immutable too, so new requirements mean a new venv — the ComfyUI requirements plus the node's
(`-r` repeats):

```sh
python3 publish.py venv <ART> rocm72-b --uv "$(command -v uv)" --python 3.12 \
    -r <core>/requirements.txt -r node-requirements.txt  …same --accelerator/--pip-arg as the venv it replaces
```

Read `freeze.txt` afterwards — a requirement without a version takes whatever is newest that day (a new major
version of a library the node was not tested with, say). On servers prefer `opencv-python-headless` to
`opencv-python`: the same `cv2`, without the desktop libraries (libGL) a fresh server does not have. The UI worker
gets its own CPU venv with the same additions.

## 4. Profiles and rollout

A profile in use cannot change: copy each profile, put in the new venv and node set, and roll the copies out — the
UI worker first (its rollout is quick, and the screen then knows the node's widgets), then the group. The rollout
verifies the node list; a node that fails to import shows as missing nodes and as `IMPORT FAILED` in the worker's log
(`/var/lib/comfyfarm/logs/<instance>.log`). Then run one real workflow with the node through the gateway.

## 5. Previews of files that are not saved

ComfyUI writes unsaved results (previews) to the executing worker's temp directory. The gateway asks the router which
worker that was for `/view?type=temp` — not for a node's own routes, which go to the UI worker and find nothing there.
VideoHelperSuite with its default *Advanced Previews: Input Only* uses `/view` for results and works; set to *Always*,
its previews of unsaved videos stay empty.

## 6. Settings of a custom node

A profile may set only the environment keys the launcher allows (contract §4: accelerator and framework settings,
never paths) — the router must not be able to switch a worker's code by setting variables. A custom node's own
setting is decided on the worker host by root, for every instance there, with a systemd drop-in (worker setup never
touches it):

```ini
# /etc/systemd/user/comfyfarm@.service.d/site.conf   (root, 0644)
[Service]
Environment=VHS_STRICT_PATHS=1
```

then, as the service account, `systemctl --user daemon-reload` and restart the instances (or let the next rollout do
it). The launcher passes its own environment through to ComfyUI.

## 7. Server paths

Users share the workers. A node that browses or loads arbitrary server paths lets every user read whatever the
service account can read on that host — through the UI worker for routes, through the GPU workers for nodes that take a
path. Turn that off where the node allows it (§6), and know which nodes cannot.

## Example: ComfyUI-VideoHelperSuite

Requirements `opencv-python`, `imageio-ffmpeg` (it brings its own ffmpeg; no system ffmpeg needed); a `web/`
directory; routes `/vhs/viewvideo`, `/vhs/queryvideo`, `/vhs/viewaudio`, `/vhs/getpath`. Node sets for both kinds
of worker (§2); venvs with `opencv-python-headless` and `imageio-ffmpeg` (§3); rollout UI worker → group (§4).
`/vhs/getpath` lists any server directory and the `…Path` nodes load any server file, so set `VHS_STRICT_PATHS=1` on
every worker host (§6): `getpath` then returns nothing outside ComfyUI's own directory, and users load videos by
uploading them (`Load Video (Upload)` — the input directory, shared storage).
