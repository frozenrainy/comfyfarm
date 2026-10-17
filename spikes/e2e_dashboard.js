const { chromium } = require("playwright-core");
// Web UI E2E through the gateway (spikes/e2e_stack.py: admin farmadmin, group "lab" + a second fake worker in group
// "rocm"). CHROMIUM_PATH=… OUT=/tmp node e2e_dashboard.js
const GW = process.env.GATEWAY || "http://127.0.0.1:18400";
const OUT = process.env.OUT || ".";
let failed = 0;
const check = (name, ok, extra = "") => { if (!ok) failed++; console.log(`${ok ? "PASS" : "FAIL"} ${name}${extra ? "  " + extra : ""}`); };
const slow = (seed) => ({ "1": { class_type: "EmptyImage", inputs: { width: 64, height: 64, batch_size: 1, color: seed } },
  "2": { class_type: "ImageBlur", inputs: { image: ["1", 0], blur_radius: 30, sigma: 1.0 } },
  "3": { class_type: "PreviewImage", inputs: { images: ["2", 0] } } });
const getJson = (p, url) => p.evaluate(async (u) => (await fetch(u)).json(), url);

async function login(p, user, pw) {
  await p.fill("input[name=username]", user); await p.fill("input[name=password]", pw);
  await Promise.all([p.waitForNavigation(), p.click("button")]);
}

(async () => {
  const b = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH });
  const ctx = await b.newContext({ viewport: { width: 1500, height: 1000 } });
  const p = await ctx.newPage();
  const errors = [];
  p.on("pageerror", (e) => errors.push(e.message));
  p.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

  // ---- login lands on the dashboard ------------------------------------------------------------------------
  await p.goto(GW + "/comfyfarm/dashboard/");
  check("unauthenticated → login with next", new URL(p.url()).pathname === "/comfyfarm/login" && p.url().includes("next="));
  await login(p, "farmadmin", "farm-e2e");
  check("back on the dashboard after login", new URL(p.url()).pathname === "/comfyfarm/dashboard/");
  await p.waitForSelector("#page .card", { timeout: 15000 });

  // ---- queue page: admin reorder, pause, note survives polling ---------------------------------------------
  await p.evaluate(async (wfs) => {
    for (const wf of wfs) await fetch("/api/prompt", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt: wf }) });
  }, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(slow));
  await p.goto(GW + "/comfyfarm/dashboard/#/queue");
  await p.waitForFunction(() => document.querySelectorAll("#page tbody tr").length >= 6, null, { timeout: 10000 });
  const order = async () => (await getJson(p, "/api/farm/queue?scope=all")).jobs.filter((j) => j.place).map((j) => j.prompt_id);
  const before = await order();
  const target = before[before.length - 2];
  const idx = await p.evaluate((pid) => [...document.querySelectorAll("#page tbody tr")].findIndex((tr) => tr._item?.prompt_id === pid), target);
  await p.locator("#page tbody tr").nth(idx).locator("button[title='Run next']").click();
  await p.waitForTimeout(1200);
  check("run next moves a job to the front", (await order())[0] === target, `queued=${before.length}`);

  const last = p.locator("#page tbody tr").last();
  await last.locator("button", { hasText: "Pause" }).click();
  await p.waitForFunction(() => [...document.querySelectorAll("#page tbody .pill")].some((x) => x.textContent === "paused"), null, { timeout: 5000 });
  check("pause from the queue page", true);
  const noteBox = p.locator("#page tbody tr").last().locator("input.note");
  await noteBox.click(); await noteBox.type("hero shot v3 — ünïcödé ✓");
  await p.waitForTimeout(5000);
  const kept = await noteBox.inputValue(), focused = await noteBox.evaluate((el) => document.activeElement === el);
  check("note survives polling while typing", kept === "hero shot v3 — ünïcödé ✓" && focused, JSON.stringify({ kept, focused }));
  await noteBox.press("Enter");
  await p.waitForTimeout(1500);
  const jobsNow = (await getJson(p, "/api/farm/queue?scope=all")).jobs;
  check("note saved", jobsNow.some((j) => j.note === "hero shot v3 — ünïcödé ✓"));
  await p.screenshot({ path: `${OUT}/ui-queue.png`, fullPage: true });

  // ---- an unreachable router is visible -------------------------------------------------------------------
  await p.route("**/api/farm/queue*", (r) => r.abort());
  await p.waitForSelector("#stale:not([hidden])", { timeout: 8000 });
  check("unreachable router shows a banner", true, await p.textContent("#stale"));
  await p.unroute("**/api/farm/queue*");
  errors.length = 0;                                               // the aborted fetches were ours
  await p.waitForSelector("#stale", { state: "hidden", timeout: 8000 });
  check("banner clears when the router answers again", true);
  await p.locator("#page tbody tr").filter({ hasText: "paused" }).locator("button", { hasText: "Resume" }).click();

  // ---- dashboard -------------------------------------------------------------------------------------------
  await p.goto(GW + "/comfyfarm/dashboard/#/");
  await p.waitForSelector("#page .stats", { timeout: 10000 });
  await p.screenshot({ path: `${OUT}/ui-dashboard.png`, fullPage: true });
  check("dashboard: groups, usage, recent", (await p.locator("#page .card").count()) === 2 && (await p.locator("#page .chart").count()) === 1);

  // ---- admin: create a user with one group --------------------------------------------------------------------
  await p.goto(GW + "/comfyfarm/dashboard/#/admin/users");
  await p.waitForSelector("#page tbody tr", { timeout: 8000 });
  await p.locator("button", { hasText: "New user" }).click();
  const dlg = p.locator("dialog.form");
  await dlg.locator("input").nth(0).fill("artist");
  await dlg.locator("input[type=password]").fill("artist-pass-1");
  await dlg.locator("label.check", { hasText: "lab" }).click();
  await dlg.locator("button[type=submit]").click();
  await p.waitForFunction(() => [...document.querySelectorAll("#page tbody tr")].some((tr) => tr.textContent.includes("artist")), null, { timeout: 5000 });
  const users = await getJson(p, "/api/admin/users");
  const artist = users.find((u) => u.username === "artist");
  check("admin creates a user with a group", artist && artist.groups.length === 1 && !artist.admin, JSON.stringify(artist));
  await p.screenshot({ path: `${OUT}/ui-admin-users.png`, fullPage: true });

  for (const tab of ["workers", "groups"]) {
    await p.goto(GW + `/comfyfarm/dashboard/#/admin/${tab}`);
    await p.waitForSelector("#page tbody tr", { timeout: 8000 });
    await p.screenshot({ path: `${OUT}/ui-admin-${tab}.png`, fullPage: true });
  }
  check("admin workers and groups pages render", true);

  // ---- statistics: presets and the 30-day limit ------------------------------------------------------------
  await p.goto(GW + "/comfyfarm/dashboard/#/admin/stats");
  await p.waitForSelector("#page .chart", { timeout: 8000 });
  await p.locator("button.seg", { hasText: "30 days" }).click();
  await p.waitForTimeout(800);
  const d = await p.locator("#page .chart .col").count();
  check("30-day statistics", d >= 30, `columns=${d}`);
  const dates = p.locator(".period input[type=date]");
  await dates.nth(0).fill("2026-01-01"); await dates.nth(1).fill("2026-03-01");
  await p.locator(".period button", { hasText: "Apply" }).click();
  await p.waitForSelector("#toast:not([hidden])", { timeout: 3000 });
  check("a range over 30 days is refused", (await p.textContent("#toast")).includes("30"), await p.textContent("#toast"));
  await p.screenshot({ path: `${OUT}/ui-admin-stats.png`, fullPage: true });

  // ---- history: search and pages ----------------------------------------------------------------------------
  // (not waitForFunction: an async predicate returns a Promise, which is truthy at once)
  for (let i = 0; i < 90 && (await getJson(p, "/api/farm/queue")).jobs.length; i++) await p.waitForTimeout(1000);
  await p.goto(GW + "/comfyfarm/dashboard/#/history");
  await p.waitForSelector("#page tbody tr", { timeout: 8000 });
  const total = await p.textContent(".pager span");
  await p.fill("input.search", "hero shot");
  await p.waitForFunction(() => document.querySelectorAll("#page tbody tr").length === 1, null, { timeout: 5000 });
  check("history search", true, `before: ${total}`);

  // ---- theme: dark, saved on the server ----------------------------------------------------------------------
  await p.goto(GW + "/comfyfarm/dashboard/#/settings");
  await p.locator("label.check", { hasText: "Dark" }).click();
  await p.waitForTimeout(800);
  await p.evaluate(() => localStorage.clear());                    // the server copy must be enough
  await p.goto(GW + "/comfyfarm/dashboard/#/");
  await p.waitForSelector("#page .card");
  await p.waitForFunction(() => document.documentElement.dataset.theme === "dark", null, { timeout: 5000 });
  check("dark theme follows the account", true);
  await p.screenshot({ path: `${OUT}/ui-dashboard-dark.png`, fullPage: true });
  await p.goto(GW + "/comfyfarm/dashboard/#/settings");
  await p.locator("label.check", { hasText: "Light" }).click();
  await p.waitForTimeout(500);
  check("no page errors (admin)", errors.length === 0, errors.join(" | "));

  // ---- the new user: own groups only, no admin pages, password change ------------------------------------------
  const ctx2 = await b.newContext({ viewport: { width: 1300, height: 900 } });
  const q = await ctx2.newPage();
  const errors2 = [];
  q.on("pageerror", (e) => errors2.push(e.message));
  await q.goto(GW + "/comfyfarm/dashboard/");
  await login(q, "artist", "artist-pass-1");
  await q.waitForSelector("#page .card", { timeout: 10000 });
  const cards = await q.locator("#page .card h3 span:first-child").allTextContents();
  check("user sees only the groups they may use", cards.length === 1 && cards[0] === "lab", JSON.stringify(cards));
  check("no System link for users", !(await q.locator("header a", { hasText: "System" }).isVisible()));
  const rocmId = (await getJson(p, "/api/admin/groups")).find((g) => g.name === "rocm").id;
  const denied = await q.evaluate(async (gid) => (await fetch("/api/prompt", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: {}, comfyfarm_group: gid }) })).status, rocmId);
  check("submitting to a group one may not use is refused", denied === 403, `status=${denied}`);
  await q.goto(GW + "/comfyfarm/dashboard/#/admin/users");
  await q.waitForTimeout(300);
  check("admin page refused to a user", (await q.textContent("#page")).includes("Administrators only"));
  await q.goto(GW + "/comfyfarm/dashboard/#/settings");
  const pw = q.locator("form input[type=password]");
  await pw.nth(0).fill("artist-pass-1"); await pw.nth(1).fill("artist-pass-2"); await pw.nth(2).fill("artist-pass-2");
  await Promise.all([q.waitForNavigation(), q.locator("form button[type=submit]").click()]);
  check("password change sends the user to log in again", q.url().includes("/comfyfarm/login") && (await q.textContent("body")).includes("Password changed"));
  await login(q, "artist", "artist-pass-2");
  check("new password works", new URL(q.url()).pathname === "/comfyfarm/dashboard/");
  await q.waitForSelector("#page .card", { timeout: 10000 });
  await q.screenshot({ path: `${OUT}/ui-user-dashboard.png`, fullPage: true });
  await q.setViewportSize({ width: 390, height: 844 });
  await q.screenshot({ path: `${OUT}/ui-user-mobile.png`, fullPage: true });
  check("no page errors (user)", errors2.length === 0, errors2.join(" | "));

  await b.close();
  console.log(failed ? `${failed} FAILED` : "ALL PASSED");
  process.exit(failed ? 1 : 0);
})();
