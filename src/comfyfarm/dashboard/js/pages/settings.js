// Personal settings: theme (saved on the server, so it follows the account), password, groups one may use.
import { act, api, applyTheme, h, state, toast } from "../core.js";

const THEMES = [["light", "Light"], ["dark", "Dark"], ["auto", "Auto (follow the system)"]];

export default {
  poll: 0,
  mount(root, { reloadMe }) {
    const me = state.me;
    const radios = THEMES.map(([v, label]) => h("label", { class: "check" },
      h("input", { type: "radio", name: "theme", value: v, checked: me.prefs.theme === v, onchange: () => {
        applyTheme(v);
        act(() => api("PUT", "/api/farm/me/prefs", { theme: v }), "Theme saved", reloadMe);
      } }), ` ${label}`));

    const cur = h("input", { type: "password", autocomplete: "current-password" });
    const nw = h("input", { type: "password", autocomplete: "new-password", minlength: "8" });
    const again = h("input", { type: "password", autocomplete: "new-password" });
    const pwForm = h("form", { class: "stack", onsubmit: async (e) => {
      e.preventDefault();
      if (nw.value !== again.value) return toast("the new passwords differ", true);
      if (nw.value.length < 8) return toast("at least 8 characters", true);
      const r = await act(() => api("POST", "/api/farm/me/password", { current: cur.value, new: nw.value }));
      if (r?.relogin) location.href = "/comfyfarm/login?m=pw&next=/comfyfarm/dashboard/";
    } },
      h("div", { class: "field" }, h("div", { class: "label" }, "Current password"), cur),
      h("div", { class: "field" }, h("div", { class: "label" }, "New password"), nw, h("div", { class: "hint" }, "at least 8 characters")),
      h("div", { class: "field" }, h("div", { class: "label" }, "New password again"), again),
      h("div", {}, h("button", { type: "submit", class: "primary" }, "Change password")),
      h("p", { class: "muted small" }, "Changing it signs this account out everywhere, this browser included."));

    root.append(
      h("section", {}, h("h2", {}, "Appearance"), h("div", { class: "panel stack" }, ...radios)),
      h("section", {}, h("h2", {}, "Password"), h("div", { class: "panel" },
        me.local ? pwForm : h("p", { class: "muted" }, "This account signs in through your organisation's login; change the password there."))),
      h("section", {}, h("h2", {}, "Worker groups you may use"), h("div", { class: "panel" },
        me.all_groups ? h("p", {}, "All groups" + (me.admin ? " (administrator)." : "."))
          : me.groups.length ? h("ul", {}, ...me.groups.map((g) => h("li", {}, g.name)))
          : h("p", { class: "muted" }, "None yet — ask an administrator."))));
    return { async refresh() {} };
  },
};
