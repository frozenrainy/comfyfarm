// Personal settings: theme and language (saved on the server, so they follow the account), password, groups one
// may use, API tokens for the command line and programs (docs/design/19).
import { act, api, applyTheme, button, copyText, formDialog, h, namedRow, setText, state, syncRows, t, table, toast, when }
  from "../core.js";
import { LANGS, locale, setLang } from "../i18n.js";

const THEMES = [["light", "Light"], ["dark", "Dark"], ["auto", "Auto (follow the system)"]];

export default {
  poll: 0,
  mount(root, { reloadMe }) {
    const me = state.me;
    const radios = THEMES.map(([v, label]) => h("label", { class: "check" },
      h("input", { type: "radio", name: "theme", value: v, checked: me.prefs.theme === v, onchange: () => {
        applyTheme(v);
        act(() => api("PUT", "/api/farm/me/prefs", { theme: v }), t("Theme saved"), reloadMe);
      } }), ` ${t(label)}`));
    const langs = LANGS.map(([v, label]) => h("label", { class: "check" },
      h("input", { type: "radio", name: "lang", value: v, checked: (me.prefs.lang || "auto") === v, onchange: async () => {
        await act(() => api("PUT", "/api/farm/me/prefs", { lang: v }));
        if (setLang(v)) location.reload();          // every page text is built in the chosen language
      } }), ` ${v === "auto" ? t(label) : label}`));

    const tokens = apiTokens();
    const cur = h("input", { type: "password", autocomplete: "current-password" });
    const nw = h("input", { type: "password", autocomplete: "new-password", minlength: "8" });
    const again = h("input", { type: "password", autocomplete: "new-password" });
    const pwForm = h("form", { class: "stack", onsubmit: async (e) => {
      e.preventDefault();
      if (nw.value !== again.value) return toast(t("the new passwords differ"), true);
      if (nw.value.length < 8) return toast(t("at least 8 characters"), true);
      const r = await act(() => api("POST", "/api/farm/me/password", { current: cur.value, new: nw.value }));
      if (r?.relogin) location.href = "/comfyfarm/login?m=pw&next=/comfyfarm/dashboard/";
    } },
      h("div", { class: "field" }, h("div", { class: "label" }, t("Current password")), cur),
      h("div", { class: "field" }, h("div", { class: "label" }, t("New password")), nw, h("div", { class: "hint" }, t("at least 8 characters"))),
      h("div", { class: "field" }, h("div", { class: "label" }, t("New password again")), again),
      h("div", {}, h("button", { type: "submit", class: "primary" }, t("Change password"))),
      h("p", { class: "muted small" }, t("Changing it signs this account out everywhere, this browser included.")));

    root.append(
      h("section", {}, h("h2", {}, t("Appearance")), h("div", { class: "panel stack" }, ...radios)),
      h("section", {}, h("h2", {}, t("Language")), h("div", { class: "panel stack" }, ...langs)),
      h("section", {}, h("h2", {}, t("Password")), h("div", { class: "panel" },
        me.local ? pwForm : h("p", { class: "muted" }, t("This account signs in through your organisation's login; change the password there.")))),
      h("section", {}, h("h2", {}, t("Worker groups you may use")), h("div", { class: "panel" },
        me.all_groups ? h("p", {}, me.admin ? t("All groups (administrator).") : t("All groups."))
          : me.groups.length ? h("ul", {}, ...me.groups.map((g) => h("li", {}, g.name)))
          : h("p", { class: "muted" }, t("None yet — ask an administrator.")))),
      tokens.el);
    return { refresh: tokens.refresh };
  },
};

// ---- API tokens ---------------------------------------------------------------------------------------------------
const SCOPES = [["read", "Read only"], ["user", "Act as me (no administration)"], ["admin", "Act as me, administration included"]];

function apiTokens() {
  let info = null;
  const tb = table([[t("Name")], [t("Scope")], [t("Token")], [t("Created")], [t("Expires")], [t("Last used")], ["", "actions"]]);
  const add = button(t("New token"), async () => {
    const scopes = SCOPES.filter(([v]) => v !== "admin" || info?.admin);
    const v = await formDialog(t("New API token"), [
      { name: "name", label: t("Name"), placeholder: t("what uses it, e.g. my laptop, render agent"), required: true },
      { name: "scope", label: t("Scope"), type: "select", value: "user", options: scopes.map(([value, label]) => ({ value, label: t(label) })) },
      { name: "days", label: t("Expires after (days)"), type: "number", value: info?.default_days ?? 90, min: 1,
        hint: t("1 to {max} days", { max: info?.max_days ?? 365 }) }], t("Create"));
    if (!v) return;
    const r = await act(() => api("POST", "/api/farm/me/tokens", { name: v.name, scope: v.scope, days: Number(v.days) }));
    if (!r) return;
    refresh();
    const code = h("pre", { class: "code" }, r.token);
    await formDialog(t("Copy the token now"), [
      { name: "x", label: t("It is shown only this once. Anyone who has it acts as you within its scope."), type: "pre", value: r.token },
      { name: "y", label: t("Use it from a terminal"), type: "pre",
        value: `comfyfarm-ctl login --url ${location.origin}\n# or: export COMFYFARM_URL=${location.origin} COMFYFARM_TOKEN=…` }],
      t("Close"), { noSubmit: true, wide: true });
    copyText(r.token, code);
  }, { cls: "primary" });
  const el = h("section", {}, h("div", { class: "section-head" }, h("h2", {}, t("API tokens")), add),
    h("p", { class: "muted small" }, t("For the comfyfarm-ctl command and other programs: a token acts as you, never with more " +
      "rights than you have. It reaches the farm's API only. Changing your password does not end your tokens — revoke " +
      "them here. A revoked token stops within 30 seconds.")), tb.el);
  const cols = ["name", "scope", "hint", "created", "expires", "used", "actions"];
  function build() {
    const tr = namedRow(cols, { actions: "actions" });
    tr.c.actions.append(button(t("Revoke"), () => {
      if (confirm(t("Revoke the token {name}? Programs using it stop within 30 seconds.", { name: tr._item.name })))
        return act(() => api("DELETE", `/api/farm/me/tokens/${tr._item.id}`), t("Revoked"), refresh);
    }, { cls: "danger" }));
    return tr;
  }
  function update(tr, k) {
    setText(tr.c.name, k.name);
    setText(tr.c.scope, t((SCOPES.find(([v]) => v === k.scope) || [k.scope, k.scope])[1]));
    setText(tr.c.hint, k.hint);
    setText(tr.c.created, when(k.created_at));
    setText(tr.c.expires, k.expired ? t("expired") : new Date(k.expires_at).toLocaleDateString(locale()));   // a future date
    tr.c.expires.className = k.expired ? "warn" : "";
    setText(tr.c.used, k.last_used_at ? when(k.last_used_at) : t("never"));
  }
  async function refresh() {
    info = await api("GET", "/api/farm/me/tokens");
    syncRows(tb.tbody, info.tokens, (k) => k.id, build, update);
  }
  return { el, refresh };
}
