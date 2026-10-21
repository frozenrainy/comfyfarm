// System → Models (docs/design/17): models from Hugging Face, downloaded and signed by the builder into the control
// plane's model store, and copied to the GPU workers of the groups that use them.
import { act, api, button, formDialog, h, namedRow, pill, setText, syncRows, t, table, toast } from "../core.js";

const size = (n) => n == null ? "—" : n >= 2 ** 30 ? `${(n / 2 ** 30).toFixed(1)} GB` : `${Math.max(1, Math.round(n / 2 ** 20))} MB`;

// "https://huggingface.co/<repo>/blob/<ref>/split_files/vae/ae.safetensors" → vae: the folder a path names, if any
function folderIn(url, folders) {
  const parts = decodeURIComponent(url.split(/[?#]/)[0]).split("/");
  return [...parts].reverse().find((p) => folders.includes(p)) || "";
}

// lines "<link>" or "<folder> <link>" → [{url, folder}], or the lines that say no folder
export function parseLines(text, folder, folders) {
  const out = [], unknown = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const m = /^([a-z_]+)\s+(\S+)$/.exec(line);
    const url = m && folders.includes(m[1]) ? m[2] : line;
    const f = m && folders.includes(m[1]) ? m[1] : folder !== "auto" ? folder : folderIn(url, folders);
    if (!f) unknown.push(line);
    out.push({ url, folder: f });
  }
  return { models: out, unknown };
}

export function models(root, { refresh }) {
  let data = null, groups = [];
  const info = h("div");
  const modelsT = table([[t("Model")], [t("Source")], [t("Size")], [t("State")], [t("Groups")], [t("Note")], ["", "actions"]]);
  const workersT = table([[t("Worker")], [t("Group")], [t("Copies")], [t("Copying")], [t("Problems")]]);
  const empty = h("p", { class: "empty muted" }, t("No models yet. Add Hugging Face links — the builder downloads them, GPU workers get copies."));
  root.append(
    h("section", {}, h("div", { class: "section-head" }, h("h2", {}, t("Models")),
      button(t("Add models"), () => add(), { cls: "primary" })), info, modelsT.el, empty),
    h("section", {}, h("div", { class: "section-head" }, h("h2", {}, t("Copies on the GPU workers"))),
      h("p", { class: "muted small" }, t("Each worker gets the models its group uses, in the background — no restart. ComfyUI lists a new one when its model list is refreshed.")),
      workersT.el));

  const groupName = (id) => groups.find((g) => g.id === id)?.name ?? `#${id}`;
  const groupBoxes = (value) => ({ name: "groups", type: "checkboxes", label: t("Groups that use them"), value: value.map(String),
                                   options: groups.map((g) => ({ value: String(g.id), label: g.name })),
                                   hint: t("their GPU workers get a copy; none checked: kept in the store only") });

  async function add() {
    const b = data?.builder;
    if (!b?.configured) return toast(t("no control-plane builder here (the compose deployment's builder service)"), true);
    if (!b.up) return toast(t("the builder is not running (docker compose ps builder)"), true);
    if (!b.models) return toast(t("the builder has no model store (COMFYFARM_MODELS, docs/guide/models.md)"), true);
    let typed = null;
    for (;;) {
      const v = await formDialog(t("Add models"), [
        { name: "links", type: "textarea", rows: 8, label: t("Hugging Face file links, one per line"), value: typed?.links ?? "",
          placeholder: "https://huggingface.co/Comfy-Org/flux1-dev/blob/main/split_files/vae/ae.safetensors",
          hint: t("as copied from the file's page; a line may start with its folder: loras https://huggingface.co/…") },
        { name: "folder", type: "select", label: t("Folder in ComfyUI"), value: typed?.folder ?? "auto",
          options: [{ value: "auto", label: t("from the link (…/split_files/vae/… → vae)") }, ...b.folders.map((f) => ({ value: f, label: f }))] },
        groupBoxes(typed?.groups ?? (groups.length === 1 ? [groups[0].id] : [])),
        { name: "note", label: t("Note") },
      ], t("Download"), { wide: true });
      if (!v) return;
      typed = v;
      const { models: list, unknown } = parseLines(v.links, v.folder, b.folders);
      if (!list.length) { toast(t("no links"), true); continue; }
      if (unknown.length) { toast(t("no folder in the link — pick one, or start the line with it: {line}", { line: unknown[0] }), true); continue; }
      const r = await act(() => api("POST", "/api/admin/models", { models: list.map((m) => ({ ...m, note: v.note.trim() || null })),
                                                                   groups: v.groups.map(Number) }),
                          (x) => t("{n} model(s) asked of the builder — it downloads them one after another", { n: x.ids.length }), refresh);
      if (r) return;
    }
  }
  async function edit(m) {
    const v = await formDialog(`${m.folder}/${m.filename}`, [groupBoxes(m.groups), { name: "note", label: t("Note"), value: m.note ?? "" }],
                               t("Save"));
    if (!v) return;
    const dropped = m.groups.filter((g) => !v.groups.includes(String(g)));
    act(() => api("PATCH", `/api/admin/models/${m.id}`, { groups: v.groups.map(Number), note: v.note }),
        dropped.length ? t("Saved — the workers of {groups} keep the copy they have", { groups: dropped.map(groupName).join(", ") }) : t("Saved"),
        refresh);
  }

  function buildModel() {
    const tr = namedRow(["model", "source", "size", "state", "groups", "note", "actions"], { actions: "actions", source: "muted small", size: "nowrap" });
    const retry = button(t("Retry"), () => act(() => api("POST", `/api/admin/models/${tr._item.id}/retry`), t("Asked again"), refresh));
    const remove = button(t("Remove"), () => {
      if (confirm(t("Remove {name} from the list? It never arrived: nothing is in the store or on a worker.", { name: tr._item.filename })))
        act(() => api("DELETE", `/api/admin/models/${tr._item.id}`), t("Removed"), refresh);
    }, { cls: "danger" });
    tr.c.actions.append(button(t("Groups"), () => edit(tr._item)), retry, remove);
    tr._failedOnly = [retry, remove];
    return tr;
  }
  function updateModel(tr, m) {
    setText(tr.c.model, `${m.folder}/${m.filename}`);
    setText(tr.c.source, `${m.repo} · ${m.path} @ ${m.revision ? m.revision.slice(0, 10) : m.ref}`);
    setText(tr.c.size, size(m.size));
    const label = m.state === "downloading" && m.size ? `${t("downloading")} ${Math.floor((100 * m.bytes) / m.size)}%` : t(m.state);
    const sig = `${m.state}|${label}|${m.error ?? ""}`;
    if (tr.c.state.dataset.v !== sig) {
      tr.c.state.dataset.v = sig;
      tr.c.state.replaceChildren(pill(m.state, label), m.error ? h("div", { class: "muted small" }, m.error) : "");
    }
    setText(tr.c.groups, m.groups.map(groupName).join(", ") || t("(none — in the store only)"));
    setText(tr.c.note, m.note ?? "");
    for (const b of tr._failedOnly) b.hidden = m.state !== "failed";
  }
  function buildWorker() { return namedRow(["name", "group", "copies", "copying", "problems"], { problems: "muted small" }); }
  function updateWorker(tr, w) {
    setText(tr.c.name, w.name);
    setText(tr.c.group, w.group_id ? groupName(w.group_id) : t("(no group)"));
    if (!w.copies) {
      setText(tr.c.copies, "—"); setText(tr.c.copying, "");
      setText(tr.c.problems, t("no model store on this host — Set up again (System → Workers) to have one"));
      return;
    }
    setText(tr.c.copies, w.wanted == null ? "…" : w.off ? t("uses the store as it is") : `${w.present ?? 0} / ${w.wanted}`);
    setText(tr.c.copying, w.copying ? `${w.copying}` + (w.total ? ` · ${size(w.bytes)} / ${size(w.total)}` : "") : "");
    const failed = Object.entries(w.failed || {}).map(([, e]) => e);
    setText(tr.c.problems, [w.error, ...failed].filter(Boolean).join(" · "));
  }

  return {
    poll: 3000,
    async refresh() {
      [data, groups] = await Promise.all([api("GET", "/api/admin/models"), api("GET", "/api/admin/groups")]);
      const b = data.builder;
      const why = !b.configured ? t("no control-plane builder here: models from the dashboard need the compose deployment's builder")
        : !b.up ? t("the builder is not running (docker compose ps builder; its log says why)")
        : !b.models ? t("the builder has no model store: set COMFYFARM_MODELS_MOUNT (docs/guide/models.md)")
        : !b.store ? t("the router does not see the model store (COMFYFARM_MODELS_DIR): GPU workers cannot copy from it")
        : "";
      const sig = `${why}|${b.hf_token}|${(b.formats || []).join()}`;
      if (info.dataset.v !== sig) {
        info.dataset.v = sig;
        info.replaceChildren(...(why ? [h("div", { class: "callout" }, why)] : [h("p", { class: "muted small" },
          (b.formats ? t("Formats: {formats} — others can run code when ComfyUI loads them.", { formats: b.formats.join(", ") }) : t("All formats allowed (COMFYFARM_MODELS_ALLOW_PICKLE=1).")) + " "
          + (b.hf_token ? t("A Hugging Face token is set for gated models.") : t("No Hugging Face token: gated models fail until one is in the builder's hf_token file.")))]));
      }
      syncRows(modelsT.tbody, data.models, (m) => m.id, buildModel, updateModel);
      empty.hidden = data.models.length > 0; modelsT.el.hidden = !data.models.length;
      syncRows(workersT.tbody, data.workers, (w) => w.id, buildWorker, updateWorker);
    },
  };
}
