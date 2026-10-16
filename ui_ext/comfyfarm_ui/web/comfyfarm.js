// ComfyFarm frontend extension (see ../__init__.py). Official extension API only — no frontend fork.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const GROUP_SETTING = "ComfyFarm.Group";
const RETRY_DELAYS_S = [1, 2, 3, 4, 5, 5, 5, 5, 5, 5];   // D-23: retry the same prompt_id, never a new one
const ATTEMPT_TIMEOUT_MS = 20000;                          // stock fetch has no timeout; a dead primary hangs minutes

let groups = [];
let submitting = false;

function currentGroup() {
  const name = app.ui.settings.getSettingValue(GROUP_SETTING, "");
  return groups.find((g) => g.name === name);
}

const origFetchApi = api.fetchApi.bind(api);

api.fetchApi = async function (route, options = {}) {
  const g = currentGroup();
  options.headers = { ...(options.headers || {}), ...(g ? { "X-ComfyFarm-Group": g.name } : {}) };
  const isSubmit = (route === "/prompt" || route === "/api/prompt") && (options.method || "GET").toUpperCase() === "POST";
  if (!isSubmit) return origFetchApi(route, options);

  if (submitting) throw new Error("ComfyFarm: a submission is still being retried");
  const body = JSON.parse(options.body);
  body.prompt_id = body.prompt_id || crypto.randomUUID();
  if (g) body.comfyfarm_group = g.id;
  options.body = JSON.stringify(body);
  submitting = true;
  try {
    for (let attempt = 0; ; attempt++) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), ATTEMPT_TIMEOUT_MS);
      try {
        const res = await origFetchApi(route, { ...options, signal: ctrl.signal });
        if (res.status !== 503 || attempt >= RETRY_DELAYS_S.length) return res;
      } catch (e) {
        if (attempt >= RETRY_DELAYS_S.length) throw e;
      } finally {
        clearTimeout(timer);
      }
      await new Promise((r) => setTimeout(r, RETRY_DELAYS_S[attempt] * 1000));
    }
  } finally {
    submitting = false;
  }
};

app.registerExtension({
  name: "comfyfarm.ui",
  async setup() {
    try {
      const r = await origFetchApi("/farm/groups");
      groups = r.ok ? await r.json() : [];
    } catch {
      groups = [];
    }
    app.ui.settings.addSetting({
      id: GROUP_SETTING,
      name: "ComfyFarm group (node definitions and where jobs run)",
      type: "combo",
      options: [{ text: "(default)", value: "" }, ...groups.map((g) => ({ text: `${g.name} — ${g.online} online`, value: g.name }))],
      defaultValue: "",
      onChange(value, old) {
        // node definitions are per group (D-20): reload so the canvas shows this group's nodes
        if (old !== undefined && value !== old) location.reload();
      },
    });
  },
});
