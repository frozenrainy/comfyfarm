# Models from the dashboard

*System → Models* adds models from Hugging Face. The builder downloads each file into the control plane's **model
store**. It pins the file to the commit its branch or tag names and checks it against the SHA-256 Hugging Face
publishes. Then it signs an entry for it, with the same key as the artifacts. The GPU workers of the groups that use a
model get a copy of it in the background ([design 17](../design/17-model-copies.md)). Each copy is checked against that
signature and SHA-256 too, and none of this needs a restart or a drain.

The site's own *Models directory* stays as it is. ComfyUI lists both, and the farm never writes into the site's
directory.

## Where the models live

- **Control plane**: `COMFYFARM_MODELS_MOUNT`.
  - Easy deployment: on by default, at `/srv/comfyfarm/model-store`.
  - Strict deployment: off until you set it in `.env`.
  - Hard disks are fine. The store holds everything ever added, so plan the space: a video model is tens of GB with its
    text encoders.
  - The builder owns the store (uid 10002). The router reads it to hand copies to the workers.
- **GPU workers**: *Model store* in *Set up a worker*. Its default is the control plane's path, the same on every host,
  like the artifact store.
  - **Artifacts copied** (the default): setup makes the directory, owned by `comfyfarm-store`. The farm copies into it
    the models the worker's group uses. Room needed: what that group uses.
  - **Artifacts mounted**: mount the control plane's model store at that path, read-only. That worker then sees every
    model in the store.
  - Left empty: no models from the dashboard on that host.
  - A worker set up before 0.1.0-dev.17 has no model store. *Set up again* (System → Workers) gives it one.
  - A one-server farm: the GPU worker's model store is the control plane's own directory. Setup sees the builder's
    files there and uses them as they are; nothing is copied.

## Adding models

*Add models*: paste Hugging Face file links, one per line, as copied from the file's page:

```
https://huggingface.co/Comfy-Org/flux1-dev/blob/main/split_files/diffusion_models/flux1-dev-fp8.safetensors
https://huggingface.co/Comfy-Org/flux1-dev/blob/main/split_files/vae/ae.safetensors
loras https://huggingface.co/<owner>/<repo>/blob/main/my-style.safetensors
```

- **Folder**:
  - With the default, *from the link*, a path through `split_files/<folder>/` (the Comfy-Org repositories) names it.
  - Otherwise pick one for the whole list, or start a line with its folder.
  - The folders are ComfyUI's own single-file model folders: `checkpoints`, `diffusion_models`, `text_encoders`,
    `vae`, `loras`, `clip_vision`, `controlnet`, `upscale_models`, `embeddings`, `style_models`, `model_patches`,
    `audio_encoders`, `latent_upscale_models`, `background_removal`, `classifiers`, `detection`, `frame_interpolation`,
    `geometry_estimation`, `gligen`, `hypernetworks`, `optical_flow`, `photomaker`, `vae_approx`. Not `diffusers`: its
    models are folders.
  - Plus the farm's own (*Folders*), such as a folder a custom node looks its models up in (`ipadapter`, `sams`, …).
    Name it as the node does: lowercase letters, digits and `_ . -`.
  - A worker lists a folder in ComfyUI after its next start. Each new folder type therefore needs a rollout or a
    restart once; new files in existing folders need nothing.
- **Groups**: their GPU workers get a copy.
  - Change them later with *Groups*, or for several models at once: tick them (a folder's box ticks all of it), then
    *Add to groups* or *Take out of groups*.
  - A group added later gets its copies in the background.
  - A group taken out drops its workers' copies.
- **Revision**:
  - The link's branch (`main`) or tag is pinned to the commit it names when the builder fetches the file.
  - A newer upload on that branch does not change what the farm has.
  - To take a newer version: add it under another file name, give it to the groups, then take the old one out of
    them.
- The builder downloads one model at a time. *State* shows `queued`, `downloading 37%`, `ready` or `failed` with the
  reason.
- An interrupted download goes on from where it stopped when retried. So does an interrupted copy to a worker.

**Copies on the GPU workers** shows, per worker, how many of its group's models it has. It also shows what is being
copied (`2 · 4.1 GB / 23.8 GB`) and anything that failed. A copy that failed is tried again every 10 minutes.

When a worker's copy arrives, the farm reads its group's node definitions again, from a worker that has the copy.
The editor shows the new model once it reloads its model lists: press `R`, or reload the page. A workflow that names
the model runs only on workers that have it. Until every worker of a group has the copy, the group's workers differ,
as *System → Workers* shows.

## Your own files

**Upload** (*System → Models → Upload*):
- Pick one file or several. Each gets a folder (the last one used is offered). Then pick the groups and *Upload all*.
- A file goes up in 16 MiB pieces. If the connection breaks, it goes on where it stopped. If the page was closed, the
  upload is listed as stopped: *Go on* and pick the same file again. An upload not finished within a day is dropped.
- The router keeps the pieces in `<model store>/.uploads/`. The builder then copies the file into place while hashing
  it, checks the format rule and signs it. The copy in the store is the builder's own, and the upload is removed.
- Big files make the store's disk busy twice, for the upload and for the builder's copy. For those, use the inbox.

**The inbox**, for big files or a control plane without internet nearby:
1. On the control-plane host, as root, copy the files into `<model store>/inbox/<folder>/`, for example
   `/srv/comfyfarm/model-store/inbox/loras/`. The builder makes the folders.
2. *Scan the inbox*.

The builder moves each file into the store, without copying, so any size is instant. It then hashes and signs it.
The models appear in the list without groups. A file that does not pass stays in the inbox, and the dashboard says
why:
- a wrong name;
- a format that can run code;
- not readable by everyone (`chmod 644`);
- a file of that name is in the store already.

## Gated models and tokens

Some repositories need you to accept their terms first (`… is gated or private`):

1. Accept them on Hugging Face with your account.
2. Make a *read* token there (Settings → Access Tokens).
3. Put it in the builder's token file. It never goes into the database or through the router:

   ```sh
   docker compose exec -u 10002 builder sh -c 'umask 077; cat > "$HOME/hf_token"'
   # paste the token, Enter, then Ctrl-D
   ```

4. *Retry* the model. The builder reads the file at each download, so no restart is needed.

*System → Models* says whether a token is set. The token goes to Hugging Face (or your mirror) only.

## Formats

Only `.safetensors`, `.sft` and `.gguf` are accepted. `.ckpt`, `.pt`, `.pth` and `.bin` are Python pickles: ComfyUI
can run code from them when it loads one. A router that someone took over must not be able to put such a file on every
worker.

The builder's setting `COMFYFARM_MODELS_ALLOW_PICKLE=1` (`.env`, then `docker compose up -d`) allows them. Like the
node pack sources, it is the builder's setting, and the dashboard cannot change it.

## A mirror, a closed network

- `COMFYFARM_HF_ENDPOINT` (`.env`) points the builder at a Hugging Face mirror that serves the same API and `resolve`
  URLs.
- A control plane without internet: carry the files in and put them in the inbox (above).

## Deleting a model

*Delete* takes a model out of the farm:
- It leaves its groups.
- The builder takes it out of the model store.
- Each worker that has a copy drops it in the background.

Jobs already running are not disturbed, because they have the model loaded. Queued jobs that name it will fail. The
same name can be added again afterwards. *Remove* on a model that never arrived only takes it off the list.

The site's own *Models directory* is never touched. Only the farm's copies are removed.

**Workers set up before 0.1.0-dev.19** take ComfyUI's older 13 folders only and cannot drop copies. *System →
Models* names them. *Set up again* (System → Workers) once, then roll out or restart them.

## Not yet

- **Free space shown before copying.** For now the host check shows the free space, and a copy that does not fit
  fails with `needs N GB more`.
- **Sources other than Hugging Face** (Civitai and others: a URL with a known SHA-256).

## When something fails

| Message | What to do |
|---|---|
| `… is gated or private` | *Gated models* above, then *Retry* |
| `no file '…' in <repo>@<ref>` | the path or the branch in the link is wrong: *Remove*, add the right one |
| `… only .safetensors, .sft, .gguf` | another file of the same model in a safe format, or *Formats* above |
| `downloading … broke off` | *Retry*: it goes on from where it stopped |
| `… not the N bytes … expected; thrown away` | the file arrived damaged or changed on the way and was thrown away — *Retry* |
| worker: `needs N GB more, … has M GB free` | free space in that host's model store; it is tried again every 10 minutes |
| worker: `its agent keeps no copies` | the host was set up before copies (0.1.0-dev.14) or with another restart backend: *Set up again* |
| worker: `no model store on this host` | *Set up again* with a *Model store* path |
| the builder has no model store | `COMFYFARM_MODELS_MOUNT` is not set (strict) or not writable by uid 10002 — the builder's log says which |
