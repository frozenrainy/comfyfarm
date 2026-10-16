const { chromium } = require("playwright-core");
// Dashboard E2E through the gateway (spikes/e2e_stack.py). CHROMIUM_PATH=… OUT=/tmp node e2e_dashboard.js
const GW = process.env.GATEWAY || "http://127.0.0.1:18400";
const OUT = process.env.OUT || ".";
const check = (name, ok, extra = "") => console.log(`${ok ? "PASS" : "FAIL"} ${name}${extra ? "  " + extra : ""}`);

(async () => {
  const b = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH });
  const p = await b.newPage({ viewport: { width: 1500, height: 1100 } });
  const errors = [];
  p.on("pageerror", (e) => errors.push(e.message));
  p.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

  await p.goto(GW + "/comfyfarm/dashboard/");
  check("unauthenticated → login with next", new URL(p.url()).pathname === "/comfyfarm/login" && p.url().includes("next="));
  await p.fill("input[name=username]", "farmadmin"); await p.fill("input[name=password]", "farm-e2e");
  await Promise.all([p.waitForNavigation(), p.click("button")]);
  check("back on the dashboard after login", new URL(p.url()).pathname === "/comfyfarm/dashboard/");
  await p.waitForSelector("#groups .card", { timeout: 15000 });

  // queue some work: 10 slow jobs (fake worker: blur radius ≥ 20 = 4 s) so there is a queue to look at
  const slow = (seed) => ({ "1": { class_type: "EmptyImage", inputs: { width: 64, height: 64, batch_size: 1, color: seed } },
    "2": { class_type: "ImageBlur", inputs: { image: ["1", 0], blur_radius: 30, sigma: 1.0 } },
    "3": { class_type: "PreviewImage", inputs: { images: ["2", 0] } } });
  const ids = await p.evaluate(async (wfs) => {
    const out = [];
    for (const wf of wfs) out.push((await (await fetch("/api/prompt", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: wf }) })).json()).prompt_id);
    return out;
  }, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(slow));
  await p.waitForFunction((n) => document.querySelectorAll("#queue tbody tr").length >= n, 4, { timeout: 10000 });

  // admin reorder: run the second-to-last queued job next
  const before = await p.evaluate(async () => (await (await fetch("/api/farm/overview?scope=all")).json()).queue.filter((j) => j.place).map((j) => j.prompt_id));
  const target = before[before.length - 2];
  await p.locator(`#queue tbody tr`).nth(await p.evaluate((pid) =>
    [...document.querySelectorAll("#queue tbody tr")].findIndex((tr) => tr._item?.prompt_id === pid), target)).locator("button[title='Run next']").click();
  await p.waitForTimeout(1500);
  const after = await p.evaluate(async () => (await (await fetch("/api/farm/overview?scope=all")).json()).queue.filter((j) => j.place).map((j) => j.prompt_id));
  check("run next moves to the front of the queue", after[0] === target, JSON.stringify({ before: before.length, first: after[0] === target }));

  // pause the last one through the UI
  const last = p.locator(`#queue tbody tr`).last();
  await last.locator("button", { hasText: "Pause" }).click();
  await p.waitForFunction(() => [...document.querySelectorAll("#queue tbody tr .pill")].some((x) => x.textContent === "paused"), null, { timeout: 5000 });
  check("pause from the dashboard", true);

  // type a note and keep focus across several refreshes: the poll must not wipe it
  const noteBox = p.locator("#queue tbody tr").last().locator("input.note");
  await noteBox.click(); await noteBox.type("hero shot v3 — ünïcödé ✓");
  await p.waitForTimeout(5000);                                   // ≥ 2 polls
  const kept = await noteBox.inputValue();
  const focused = await noteBox.evaluate((el) => document.activeElement === el);
  check("note survives polling while typing", kept === "hero shot v3 — ünïcödé ✓" && focused, JSON.stringify({ kept, focused }));
  await noteBox.press("Enter");
  await p.waitForTimeout(2500);
  const saved = await p.evaluate(async (pid) => {
    const o = await (await fetch("/api/farm/overview?scope=all")).json();
    return (o.queue.find((j) => j.prompt_id === pid) || {}).note;
  }, ids[ids.length - 1]);
  check("note saved", saved === "hero shot v3 — ünïcödé ✓", JSON.stringify(saved));

  await p.screenshot({ path: `${OUT}/dashboard-busy.png`, fullPage: true });

  // observation failure must be visible, not look like an idle farm
  await p.route("**/api/farm/overview*", (r) => r.abort());
  await p.waitForSelector("#stale:not([hidden])", { timeout: 8000 });
  check("unreachable router shows a banner", true, await p.textContent("#stale"));
  await p.screenshot({ path: `${OUT}/dashboard-stale.png` });
  await p.unroute("**/api/farm/overview*");
  errors.length = 0;                                               // the aborted fetches were ours
  await p.waitForSelector("#stale", { state: "hidden", timeout: 8000 });
  check("banner clears when the router answers again", true);

  // resume, wait for everything to finish, then retry one from "Recent"
  await p.locator("#queue tbody tr").filter({ hasText: "paused" }).locator("button", { hasText: "Resume" }).click();
  await p.waitForFunction(() => document.querySelectorAll("#queue tbody tr").length === 0, null, { timeout: 60000 });
  const nRecent = await p.locator("#recent tbody tr").count();
  await p.locator("#recent tbody tr").first().locator("button", { hasText: "Retry" }).click();
  await p.waitForFunction(() => document.querySelectorAll("#queue tbody tr").length === 1, null, { timeout: 5000 });
  check("retry from recent queues a new job", true, `recent=${nRecent}`);
  await p.waitForFunction(() => document.querySelectorAll("#queue tbody tr").length === 0, null, { timeout: 30000 });
  await p.screenshot({ path: `${OUT}/dashboard-idle.png`, fullPage: true });

  await p.setViewportSize({ width: 390, height: 844 });
  await p.screenshot({ path: `${OUT}/dashboard-mobile.png`, fullPage: true });
  check("no page errors", errors.length === 0, errors.join(" | "));
  await b.close();
})();
