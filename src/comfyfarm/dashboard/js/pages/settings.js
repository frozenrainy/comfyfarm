// Personal settings: theme and language (saved on the server, so they follow the account), password, groups one
// may use.
import { act, api, applyTheme, h, state, t, toast } from "../core.js";
import { LANGS, setLang } from "../i18n.js";

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
          : h("p", { class: "muted" }, t("None yet — ask an administrator.")))));
    return { async refresh() {} };
  },
};
