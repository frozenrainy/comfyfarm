const { chromium } = require("playwright-core");
// Workflow tab E2E: ComfyUI inside the dashboard, group picker next to Run, queue badge, login user list.
// spikes/e2e_stack.py --gpu <fake cuda> --gpu2 <fake rocm> --ui <stock ComfyUI with ui_ext>; CHROMIUM_PATH=… OUT=/tmp node e2e_workflow.js
const GW = process.env.GATEWAY || "http://127.0.0.1:18400";
const OUT = process.env.OUT || ".";
let failed = 0;
const check = (name, ok, extra = "") => { if (!ok) failed++; console.log(`${ok ? "PASS" : "FAIL"} ${name}${extra ? "  " + extra : ""}`); };
const getJson = (p, url) => p.evaluate(async (u) => (await fetch(u)).json(), url);
const slow = { "1": { class_type: "EmptyImage", inputs: { width: 64, height: 64, batch_size: 1, color: 1 } },
  "2": { class_type: "ImageBlur", inputs: { image: ["1", 0], blur_radius: 30, sigma: 1.0 } },
  "3": { class_type: "PreviewImage", inputs: { images: ["2", 0] } } };

async function comfyFrame(p) {
  const frame = await (await p.waitForSelector("#workflow iframe", { timeout: 10000 })).contentFrame();
  await frame.waitForFunction(() => window.app && window.app.graph && window.app.canvas, null, { timeout: 60000 });
  await frame.waitForSelector("[data-testid=action-bar-buttons] button", { timeout: 15000 });
  return frame;
}

(async () => {
  const b = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH });
  const p = await (await b.newContext({ viewport: { width: 1500, height: 950 } })).newPage();
  const errors = [];
  p.on("pageerror", (e) => errors.push(e.message));

  await p.goto(GW + "/comfyfarm/login");
  check("login page: a text field by default", await p.locator("input[name=username]").count() === 1);
  await p.fill("input[name=username]", "farmadmin"); await p.fill("input[name=password]", "farm-e2e");
  await Promise.all([p.waitForNavigation(), p.click("button")]);
  check("login lands on the dashboard", new URL(p.url()).pathname === "/comfyfarm/dashboard/");

  // ---- Workflow tab ----------------------------------------------------------------------------------------------
  await p.click("header a[data-page=comfyui]");
  let frame = await comfyFrame(p);
  check("ComfyUI runs inside the Workflow tab", true);
  check("the ComfyFarm header stays", await p.locator("header nav").isVisible());
  const label = () => frame.locator("[data-testid=action-bar-buttons] button").first().textContent();
  check("group button next to Run, site default first", (await label()).includes("Group: default"), await label());
  await p.screenshot({ path: `${OUT}/ui-workflow.png` });

  // pick a group → the frame reloads with that group's node list
  await frame.locator("[data-testid=action-bar-buttons] button").first().click();
  await frame.waitForSelector("#comfyfarm-group-picker", { timeout: 5000 });
  const choices = await frame.locator("#comfyfarm-group-picker button").allTextContents();
  check("picker lists the groups one may use", choices.some((t) => t.includes("rocm")) && choices.some((t) => t.includes("lab")), JSON.stringify(choices));
  await p.screenshot({ path: `${OUT}/ui-workflow-picker.png` });
  await Promise.all([frame.waitForNavigation(), frame.locator("#comfyfarm-group-picker button", { hasText: "rocm" }).click()]);
  frame = await comfyFrame(p);
  check("after picking: Group: rocm", (await label()).includes("Group: rocm"), await label());
  check("choice kept in localStorage", (await frame.evaluate(() => localStorage.getItem("comfyfarm.workflowGroup"))).includes("rocm"));

  // the frame survives tab switches (state, websocket, unsaved workflow)
  await frame.evaluate(() => { window.__comfyfarmMarker = 42; });
  await p.click("header a[data-page='']");
  await p.waitForSelector("#page .card", { timeout: 10000 });
  check("dashboard shown, frame hidden", await p.locator("#workflow").isHidden());
  await p.click("header a[data-page=comfyui]");
  check("back on Workflow: the same ComfyUI (not reloaded)", await frame.evaluate(() => window.__comfyfarmMarker) === 42);

  // submit from ComfyUI: lands in the picked group, and the header badge follows at once (message, not the 5 s poll)
  await p.waitForTimeout(5500);                                   // one regular badge poll with nothing queued
  const before = await p.locator("#qbadge").isHidden();             // rendered state, not the attribute
  const t0 = Date.now();
  await frame.evaluate(async (wf) => { await window.app.loadApiJson(wf, "e2e.json"); await window.app.queuePrompt(0, 1); }, slow);
  await p.locator("#qbadge").waitFor({ state: "visible", timeout: 4000 });
  const ms = Date.now() - t0;
  check("queue badge updated right after Run", before && ms < 1000, `${ms} ms, badge ${await p.textContent("#qbadge")}`);
  const q = await getJson(p, "/api/farm/queue");
  const groups = await getJson(p, "/api/farm/groups");
  const rocm = groups.find((g) => g.name === "rocm");
  check("the job went to the picked group", q.jobs.length === 1 && q.jobs[0].group_id === rocm.id, JSON.stringify(q.jobs.map((j) => j.group)));
  await p.screenshot({ path: `${OUT}/ui-workflow-running.png` });
  check("ComfyUI ↗ opens bare ComfyUI in a new tab",
        await p.locator("header a", { hasText: "ComfyUI ↗" }).getAttribute("target") === "_blank");

  // ---- System → Settings: user list on the login page ------------------------------------------------------------
  await p.goto(GW + "/comfyfarm/dashboard/#/admin/settings");
  await p.locator("label.check", { hasText: "user name from a list" }).click();
  await p.waitForSelector("#toast:not([hidden])", { timeout: 5000 });
  const anon = await (await b.newContext()).newPage();
  await anon.goto(GW + "/comfyfarm/login");
  const opts = await anon.locator("select[name=username] option").allTextContents();
  check("login page offers the user names once enabled", opts.includes("farmadmin"), JSON.stringify(opts));
  await anon.screenshot({ path: `${OUT}/ui-login-list.png` });
  await anon.selectOption("select[name=username]", "farmadmin");
  await anon.fill("input[name=password]", "farm-e2e");
  await Promise.all([anon.waitForNavigation(), anon.click("button")]);
  check("…and logging in with it works", new URL(anon.url()).pathname === "/comfyfarm/dashboard/");
  await p.locator("label.check", { hasText: "user name from a list" }).click();      // back off
  await p.waitForTimeout(800);

  check("no page errors in the dashboard", errors.length === 0, errors.join(" | "));
  await b.close();
  console.log(failed ? `${failed} FAILED` : "ALL PASSED");
  process.exit(failed ? 1 : 0);
})();
