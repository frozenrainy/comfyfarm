// System → Models (docs/design/17): models from Hugging Face, downloaded and signed by the builder into the control
// plane's model store, and copied to the GPU workers of the groups that use them.
import { act, api, bar, button, formDialog, h, namedRow, pill, setText, syncRows, t, table, toast } from "../core.js";
import { bytes, pickFiles, uploadFile } from "../upload.js";

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

// files picked for upload in this page (kept across refreshes): {file, folder, state, received, error, cancel}
const staged = [];

export function models(root, { refresh }) {
  let data = null, groups = [], server = [];
  const info = h("div");
  const uploadsEl = h("div"), inboxEl = h("div");
  // several models into or out of groups at once (field: an inbox of many files, each its own Groups dialog)
  const selected = new Set();
  const allBox = h("input", { type: "checkbox", title: t("Select all"), onchange: (e) => {
    for (const m of data?.models || []) e.target.checked ? selected.add(m.id) : selected.delete(m.id);
    syncRows(modelsT.tbody, data.models, (m) => m.id, buildModel, updateModel); renderBulk();
  } });
  const modelsT = table([[allBox, "pick"], [t("Model")], [t("Source")], [t("Size")], [t("State")], [t("Groups")], [t("Note")], ["", "actions"]]);
  const bulkEl = h("div");
  const workersT = table([[t("Worker")], [t("Group")], [t("Copies")], [t("Copying")], [t("Problems")]]);
  const empty = h("p", { class: "empty muted" }, t("No models yet. Add Hugging Face links — the builder downloads them, GPU workers get copies."));
  root.append(
    h("section", {}, h("div", { class: "section-head" }, h("h2", {}, t("Models")),
      button(t("Add models"), () => add(), { cls: "primary" }), button(t("Upload"), () => pick()),
      button(t("Scan the inbox"), () => scan())), info, uploadsEl, inboxEl, bulkEl, modelsT.el, empty),
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
  // ---- uploads (docs/design/18): the operator's own files, in chunks, resumable ----------------------------------
  const accept = () => (data?.builder?.formats || []).join(",");
  let batchGroups = null;
  async function pick() {
    const b = data?.builder;
    if (!b?.up || !b.models) return toast(t("the builder is not running, or has no model store (docs/guide/models.md)"), true);
    if (!data.uploads) return toast(t("the router cannot write the model store's .uploads directory (docs/guide/models.md)"), true);
    const files = await pickFiles({ multiple: true, accept: accept() });
    const last = localStorage.getItem("comfyfarm.uploadFolder") || "loras";
    for (const file of files) staged.push({ file, folder: last, state: "staged", received: 0 });
    renderUploads();
  }
  async function startAll() {
    localStorage.setItem("comfyfarm.uploadFolder", staged.find((x) => x.state === "staged")?.folder || "loras");
    for (const x of staged.filter((y) => y.state === "staged" || y.state === "failed")) {
      x.state = "uploading"; x.error = null; renderUploads();
      try {
        await uploadFile(x.file, "model", { folder: x.folder, groups: (batchGroups || []).map(Number) },
                         (n) => { x.received = n; renderUploads(); }, x.resume || null, () => x.state === "cancelled");
        x.state = "done";
      } catch (e) {
        if (x.state !== "cancelled") { x.state = "failed"; x.error = e.message; }
      }
      renderUploads();
    }
    refresh();
  }
  async function resume(u) {
    const [file] = await pickFiles({ accept: accept() });
    if (!file) return;
    if (file.name !== u.filename || file.size !== u.size) return toast(t("not the same file: {name}, {size} bytes", { name: u.filename, size: u.size }), true);
    staged.push({ file, folder: u.meta.folder, state: "staged", received: u.received, resume: { ...u, chunk: 16 << 20 } });
    renderUploads();
    startAll();
  }
  let uploadsSig = "";
  function renderUploads() {
    const sig = JSON.stringify([staged.map((x) => [x.file.name, x.folder, x.state, x.received, x.error]),
                                server.map((u) => [u.id, u.received]), (batchGroups || []).join()]);
    if (sig === uploadsSig) return;
    uploadsSig = sig;
    const folders = data?.builder?.folders || [];
    const rows = staged.map((x, i) => h("tr", {},
      h("td", {}, x.file.name), h("td", { class: "num nowrap" }, bytes(x.file.size)),
      h("td", {}, x.state === "staged"
        ? h("select", { onchange: (e) => { x.folder = e.target.value; } }, ...folders.map((f) => h("option", { value: f, selected: f === x.folder }, f)))
        : x.folder),
      h("td", { class: "nowrap" }, x.state === "uploading" ? bar(x.received / x.file.size) : pill(x.state === "staged" ? "pending" : x.state === "done" ? "ready" : x.state, t(x.state)),
        x.error ? h("div", { class: "muted small" }, x.error) : ""),
      h("td", { class: "actions" }, x.state === "uploading"
        ? button(t("Cancel"), () => { x.state = "cancelled"; renderUploads(); })
        : x.state !== "done" ? button("✕", () => { staged.splice(i, 1); renderUploads(); }, { title: t("Remove from the list") }) : "")));
    const mine = new Set(staged.filter((x) => x.resume).map((x) => x.resume.id));
    const left = server.filter((u) => u.kind === "model" && u.state === "uploading" && !mine.has(u.id)
                                       && !staged.some((x) => x.state === "uploading" && x.file.name === u.filename));
    const rest = left.map((u) => h("tr", {},
      h("td", {}, u.filename), h("td", { class: "num nowrap" }, bytes(u.size)), h("td", {}, u.meta.folder || ""),
      h("td", { class: "nowrap" }, t("{done} of {total} arrived — stopped", { done: bytes(u.received), total: bytes(u.size) })),
      h("td", { class: "actions" }, button(t("Go on"), () => resume(u), { title: t("pick the same file again") }),
        button(t("Cancel"), () => act(() => api("DELETE", `/api/admin/uploads/${u.id}`), t("Cancelled"), refresh), { cls: "danger" }))));
    if (!rows.length && !rest.length) return uploadsEl.replaceChildren();
    const waiting = staged.some((x) => x.state === "staged" || x.state === "failed");
    uploadsEl.replaceChildren(h("div", { class: "callout" },
      h("strong", {}, t("Uploads")), " ",
      h("span", { class: "muted small" }, t("each file goes up in pieces; a broken connection goes on where it stopped")),
      h("table", {}, h("tbody", {}, ...rows, ...rest)),
      waiting ? h("div", { class: "checks" }, h("span", { class: "muted" }, t("Groups that use them") + ": "),
        ...groups.map((g) => h("label", { class: "check" }, h("input", { type: "checkbox", checked: (batchGroups || []).includes(String(g.id)),
          onchange: (e) => { batchGroups = [...new Set([...(batchGroups || []).filter((v) => v !== String(g.id)), ...(e.target.checked ? [String(g.id)] : [])])]; } }),
          ` ${g.name}`))) : "",
      waiting ? h("div", { class: "dialog-actions" }, button(t("Clear"), () => { for (let i = staged.length - 1; i >= 0; i--) if (staged[i].state !== "uploading") staged.splice(i, 1); renderUploads(); }),
        button(t("Upload all"), () => startAll(), { cls: "primary" })) : ""));
  }

  // ---- the inbox ---------------------------------------------------------------------------------------------
  async function scan() {
    if (!data?.builder?.inbox) return toast(t("the builder has no model store (COMFYFARM_MODELS, docs/guide/models.md)"), true);
    const v = await formDialog(t("Scan the inbox"), [
      { ...groupBoxes(groups.length === 1 ? [groups[0].id] : []), hint: t("every model this scan takes in goes into these groups; none checked: no groups yet") },
    ], t("Scan"));
    if (!v) return;
    act(() => api("POST", "/api/admin/models/inbox", { groups: v.groups.map(Number) }),
        t("The builder takes in what the inbox holds — the models appear below"), refresh);
  }

  function renderBulk() {
    allBox.checked = !!data?.models?.length && data.models.every((m) => selected.has(m.id));
    allBox.indeterminate = selected.size > 0 && !allBox.checked;
    if (!selected.size) return bulkEl.replaceChildren();
    const ids = () => [...selected];
    const change = async (how) => {
      const v = await formDialog(how === "add" ? t("Add {n} model(s) to groups", { n: selected.size }) : t("Take {n} model(s) out of groups", { n: selected.size }),
        [{ ...groupBoxes([]), label: t("Groups"), hint: how === "add" ? t("their GPU workers get copies in the background")
                                                                       : t("the workers of those groups keep the copies they have") }],
        how === "add" ? t("Add") : t("Take out"));
      if (!v || !v.groups.length) return;
      act(() => api("POST", "/api/admin/models/groups", { models: ids(), [how]: v.groups.map(Number) }),
          (r) => how === "add" ? t("{n} model(s) added to groups", { n: r.models }) : t("{n} model(s) taken out of groups", { n: r.models }),
          refresh);
    };
    bulkEl.replaceChildren(h("div", { class: "bulk" }, h("strong", {}, t("{n} selected", { n: selected.size })),
      button(t("Add to groups"), () => change("add"), { cls: "primary" }), button(t("Take out of groups"), () => change("remove")),
      button(t("Clear selection"), () => { selected.clear(); syncRows(modelsT.tbody, data.models, (m) => m.id, buildModel, updateModel); renderBulk(); })));
  }
  let inboxSig = "";
  function renderInbox() {
    const x = data?.inbox, path = data?.builder?.inbox;
    const sig = JSON.stringify([x, path]);
    if (sig === inboxSig) return;
    inboxSig = sig;
    const how = path ? h("p", { class: "muted small" }, t("Inbox: files copied (as root) into {path}/<folder>/ on the control-plane host, then Scan the inbox.", { path })) : "";
    if (!x) return inboxEl.replaceChildren(how);
    const bad = (x.results || []).filter((r) => r.error), ok = (x.results || []).filter((r) => !r.error);
    inboxEl.replaceChildren(how, h("p", { class: "small" },
      x.state === "done" ? t("Last scan: {n} taken in", { n: ok.length }) + (bad.length ? " · " + t("{n} left in the inbox", { n: bad.length }) : "")
      : x.state === "failed" ? t("Last scan failed: {error}", { error: x.error || "" })
      : t("Scanning the inbox…") + (x.model ? ` ${x.model}` : "")),
      ...bad.map((r) => h("div", { class: "muted small" }, `${r.folder}/${r.filename}: ${r.error}`)));
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
    const tr = namedRow(["pick", "model", "source", "size", "state", "groups", "note", "actions"], { pick: "pick", actions: "actions", source: "muted small", size: "nowrap" });
    tr.pick = h("input", { type: "checkbox", onchange: (e) => { e.target.checked ? selected.add(tr._item.id) : selected.delete(tr._item.id); renderBulk(); } });
    tr.c.pick.append(tr.pick);
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
    tr.pick.checked = selected.has(m.id);
    setText(tr.c.model, `${m.folder}/${m.filename}`);
    setText(tr.c.source, m.source === "upload" ? t("uploaded by {who}", { who: m.created_by || "?" })
      : m.source === "inbox" ? t("from the inbox") : `${m.repo} · ${m.path} @ ${m.revision ? m.revision.slice(0, 10) : m.ref}`);
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
      [data, groups, server] = await Promise.all([api("GET", "/api/admin/models"), api("GET", "/api/admin/groups"),
                                                  api("GET", "/api/admin/uploads").catch(() => [])]);
      renderUploads();
      renderInbox();
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
      for (const id of [...selected]) if (!data.models.some((m) => m.id === id)) selected.delete(id);      // gone since
      syncRows(modelsT.tbody, data.models, (m) => m.id, buildModel, updateModel);
      renderBulk();
      empty.hidden = data.models.length > 0; modelsT.el.hidden = !data.models.length;
      syncRows(workersT.tbody, data.workers, (w) => w.id, buildWorker, updateWorker);
    },
  };
}
