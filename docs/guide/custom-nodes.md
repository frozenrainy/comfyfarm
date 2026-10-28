# Adding custom nodes

A custom node reaches the workers the same way ComfyUI itself does: as published artifacts, tied together by a
profile and rolled out (rollouts.md). Nothing is installed on a worker by hand, and ComfyUI-Manager is not used —
every worker of a group must run exactly the same nodes. Worked example at the end: ComfyUI-VideoHelperSuite.

**With the compose deployment** (its builder, [design 15](../design/15-control-plane-builds.md)):

1. *System → Rollout → Node packs → Add node pack*: the repository and a branch or tag (empty: the default branch). The
   builder looks up the commit it names now; the pack stays pinned to it until *Update*.
2. Untick *Also on the UI worker* for a pack that imports only with a GPU. Add a constraint where a pack breaks on a
   newer dependency (`kornia<0.8` for ComfyUI-LTXVideo); name another requirements file if the pack ships a lighter one.
3. *Build* (your kind of GPU, both halves, with the node packs) → *From a recipe* → roll the UI profile out, then the
   group's. A pack that fails to import stops that worker's step and is named (§4).

**A pack that is in no repository** (a studio's own tools, a pack handed over as a zip):
- *Node packs → Upload an archive* takes a `.zip`, `.tar.gz`, `.tgz` or `.tar` of up to 2 GiB. GitHub's
  *Download ZIP* works as it is: its single top-level directory is taken as the pack.
- The pack is pinned to the archive's SHA-256, as a git pack is pinned to its commit. *Update* takes its next version as
  another archive; the name and the settings stay. Do not add the next version as a second pack: two copies of one pack
  register the same nodes and routes, and ComfyUI stops at start on every worker of the set. The build refuses two
  packs from the same repository, or two archives that unpack from the same top-level folder ([D-49](../design/decisions.md)).
- The builder unpacks it when it builds, and only regular files and directories come out: absolute paths, `..`,
  links and devices refuse the archive, and `.git` is left out ([design 18](../design/18-uploads.md)).
- Archives skip the builder's allowed sources, so they need its own switch: `COMFYFARM_BUILD_NODE_ARCHIVES=1`. The
  easy deployment has it on; turn it off with an empty value in `.env`. The strict deployment has it off.

The pack's requirements, one OpenCV, submodules and the rest are handled by the build (rollouts.md §1, *Custom nodes in
a recipe*). Read §1, §5–§7 below anyway: what a node does with routes, previews, settings and server paths is the same
however it was installed. The rest of this page is the way without the builder.

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
UI worker first (its rollout is quick, and the screen then knows the node's widgets), then the group. A node that
fails to import does not stop ComfyUI — the worker would run on without it — so the rollout asks the worker extension
which custom nodes ComfyUI dropped (its own verdict, read from its log: an exception, no node mappings, a failed V3
entrypoint), and a single one fails that worker's step: it goes back to its previous profile (the error names the
node; the cause is under `IMPORT FAILED` in `/var/lib/comfyfarm/logs/<instance>.log`). Packs a profile disables on
purpose (`--disable-all-custom-nodes` with a whitelist) are not failures. With `--verbose` above INFO the verdict is
not in the log and the extension reports it as unknown — the rollout then cannot check it. A worker started outside a
rollout shows the same as a warning in System → Workers. Then run one real workflow with the node through the gateway.

**System libraries.** Some packs' wheels load desktop libraries a minimal server lacks (pymeshlab, used by 3D packs,
needs nine). A rollout names the missing one and its package — `ComfyUI-Hunyuan3DWrapper (libGL.so.1 is missing on the
host — install libgl1)`. On Debian/Ubuntu these cover the packs seen so far:

```sh
sudo apt install libgl1 libopengl0 libegl1 libx11-6 libxcb1 libxext6 libxrender1 libsm6 libfontconfig1 libfreetype6 \
    libcom-err2 libgpg-error0 libp11-kit0
```

**Installs at run time.** A few packs pip-install modules while ComfyUI imports them. The launcher starts ComfyUI with
`PIP_NO_INDEX=1` and published venvs are read-only, so such an install fails at once instead of waiting for a network;
name the pack's files in `requirements_file` so the build puts them in (AlekPet keeps one per node).

## 5. Previews of files that are not saved

ComfyUI writes unsaved results (previews) to the executing worker's temp directory. The gateway asks the router which
worker that was for `/view?type=temp` — not for a node's own routes, which go to the UI worker and find nothing there.
VideoHelperSuite with its default *Advanced Previews: Input Only* uses `/view` for results and works; set to *Always*,
its previews of unsaved videos stay empty.

## 6. Settings of a custom node

A profile may set only the environment keys the launcher allows (contract §4: accelerator and framework settings,
never paths) — the router must not be able to switch a worker's code by setting variables. A custom node's own
setting is decided where the worker runs, outside the router's reach:

- **GPU worker hosts**: a line in `/etc/comfyfarm/<instance>.env` (root's; setup writes `VHS_STRICT_PATHS=1` there by
  default) or, to survive *Set up again*, a systemd drop-in for every instance on the host:

  ```ini
  # /etc/systemd/user/comfyfarm@.service.d/site.conf   (root, 0644)
  [Service]
  Environment=SOME_NODE_SETTING=1
  ```

  then `sudo systemctl --user -M comfyfarm@ daemon-reload` and *Restart* the worker in System → Rollout.
- **The UI worker container**: an `environment:` entry of the `ui` service in `compose.override.yaml`, then
  `docker compose up -d ui` (the compose file sets `VHS_STRICT_PATHS: "1"` already).

The launcher passes its own environment through to ComfyUI.

## 7. Server paths

Users share the workers. A node that browses or loads arbitrary server paths lets every user read whatever the
service account can read on that host — through the UI worker for routes, through the GPU workers for nodes that take a
path. Turn that off where the node allows it (§6), and know which nodes cannot.

## Example: ComfyUI-VideoHelperSuite

With the compose deployment: *System → Rollout → Node packs → Add node pack*, repository
`https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite`, branch empty, *Also on the UI worker* ticked (it has web
code and routes) → *Build* → *From a recipe* → roll the UI profile out, then the group's. Search "VHS" in ComfyUI's
node list to see it.

What the build takes care of: its requirements `opencv-python` (becomes the one headless OpenCV of the venv) and
`imageio-ffmpeg` (it brings its own ffmpeg; no system ffmpeg needed). What it has: a `web/` directory and routes
`/vhs/viewvideo`, `/vhs/queryvideo`, `/vhs/viewaudio`, `/vhs/getpath`. `/vhs/getpath` lists any server directory and
the `…Path` nodes load any server file; `VHS_STRICT_PATHS=1` — set by default on the UI worker container and by setup
on GPU hosts (§6) — makes `getpath` return nothing outside ComfyUI's own directory, and users load videos by uploading
them (`Load Video (Upload)` — the input directory, shared storage). By hand, without the builder: node sets for both
kinds of worker (§2), venvs with `opencv-python-headless` and `imageio-ffmpeg` (§3), rollout UI worker → group (§4).
