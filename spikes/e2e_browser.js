const { chromium } = require("playwright-core");
// Browser E2E: stock ComfyUI frontend through the gateway (spikes/e2e_stack.py). CHROMIUM_PATH=… node e2e_browser.js
const GW = process.env.GATEWAY || "http://127.0.0.1:18400";
(async () => {
  const b = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH });
  const p = await b.newPage({ viewport: { width: 1600, height: 1000 } });
  const posted = [];
  p.on("request", (r) => { if (r.url().endsWith("/api/prompt") && r.method() === "POST") posted.push(JSON.parse(r.postData())); });
  const viewStatus = [];
  p.on("response", (r) => { if (r.url().includes("/api/view")) viewStatus.push(r.status()); });
  await p.goto(GW + "/");
  console.log("redirected to:", new URL(p.url()).pathname);
  await p.fill("input[name=username]", "farmadmin"); await p.fill("input[name=password]", "farm-e2e");
  await Promise.all([p.waitForNavigation(), p.click("button")]);
  await p.waitForFunction(() => window.app && window.app.graph && window.app.canvas, null, { timeout: 90000 });
  await p.waitForTimeout(3000);
  const r = await p.evaluate(async () => {
    const app = window.app, api = window.comfyAPI?.api?.api || app.api;
    const seen = [];
    for (const t of ["execution_start", "executing", "progress_state", "executed", "execution_success", "execution_error", "status", "b_preview"]) {
      api.addEventListener(t, (e) => seen.push(t));
    }
    const exts = app.extensions.map((e) => e.name).filter((n) => n.includes("comfyfarm"));
    const setting = app.ui.settings.getSettingValue("ComfyFarm.Group", "(missing)");
    const wf = {"1": {"class_type": "EmptyImage", "inputs": {"width": 512, "height": 512, "batch_size": 1, "color": 16744448}},
                "2": {"class_type": "ImageBlur", "inputs": {"image": ["1", 0], "blur_radius": 5, "sigma": 1.0}},
                "3": {"class_type": "PreviewImage", "inputs": {"images": ["2", 0]}}};
    await app.loadApiJson(wf, "e2e.json");
    await app.queuePrompt(0, 1);
    const t0 = Date.now();
    while (!seen.includes("execution_success") && !seen.includes("execution_error") && Date.now() - t0 < 60000) await new Promise((r) => setTimeout(r, 200));
    await new Promise((r) => setTimeout(r, 2000));
    const node = app.graph._nodes.find((n) => n.type === "PreviewImage");
    const imgs = (node?.imgs || []).map((i) => i.src);
    const q = await (await api.fetchApi("/queue")).json();
    const hist = await (await api.fetchApi("/history")).json();
    return { exts, setting, seen: [...new Set(seen)], counts: seen.reduce((m, t) => (m[t] = (m[t] || 0) + 1, m), {}), imgs,
             queue: [q.queue_running.length, q.queue_pending.length], history: Object.keys(hist).length, secs: (Date.now() - t0) / 1000 };
  });
  console.log(JSON.stringify(r, null, 1));
  console.log("POST /api/prompt bodies:", posted.map((x) => ({ prompt_id: x.prompt_id, comfyfarm_group: x.comfyfarm_group, client_id: x.client_id })));
  console.log("/api/view statuses:", viewStatus);
  await p.screenshot({ path: "/tmp/e2e_shot.png" });
  await b.close();
})();
