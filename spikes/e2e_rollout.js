const { chromium } = require("playwright-core");
// Rollout UI E2E: spikes/e2e_stack.py --rollout-demo 2 (fake hosts restarted like the launcher does).
// CHROMIUM_PATH=… OUT=/tmp node e2e_rollout.js
const GW = process.env.GATEWAY || "http://127.0.0.1:18400";
const OUT = process.env.OUT || ".";
let failed = 0;
const check = (name, ok, extra = "") => { if (!ok) failed++; console.log(`${ok ? "PASS" : "FAIL"} ${name}${extra ? "  " + extra : ""}`); };
const getJson = (p, url) => p.evaluate(async (u) => (await fetch(u)).json(), url);

async function waitRollout(p, timeout = 120000) {
  const t0 = Date.now();
  let shot = false;
  while (Date.now() - t0 < timeout) {
    const [r] = await getJson(p, "/api/admin/rollouts?limit=1");
    if (!shot && r.steps.some((s) => ["restarting", "starting"].includes(s.status))) {
      await p.screenshot({ path: `${OUT}/ui-rollout-running-${r.id}.png`, fullPage: true });
      shot = true;
    }
    if (!["running", "aborting"].includes(r.status)) return r;
    await p.waitForTimeout(500);
  }
  throw new Error("rollout did not finish");
}

async function startRollout(p, profileName) {
  const before = (await getJson(p, "/api/admin/rollouts?limit=1"))[0]?.id ?? 0;
  await p.locator("button", { hasText: "Start rollout" }).click();
  const dlg = p.locator("dialog.form");
  await dlg.locator("select").first().selectOption({ label: profileName });       // exact: "next" ≠ "next-broken"
  await dlg.locator("button[type=submit]").click();
  for (let i = 0; i < 50; i++) {                       // the new rollout, not a toast left over from before
    const [r] = await getJson(p, "/api/admin/rollouts?limit=1");
    if (r && r.id > before) return r.profile;
    await p.waitForTimeout(100);
  }
  return null;
}

(async () => {
  const b = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH });
  const p = await (await b.newContext({ viewport: { width: 1500, height: 1100 } })).newPage();
  const errors = [];
  p.on("pageerror", (e) => errors.push(e.message));
  p.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  p.on("dialog", (d) => d.accept());                                  // confirm()/alert()

  await p.goto(GW + "/comfyfarm/dashboard/#/admin/rollout");
  await p.fill("input[name=username]", "farmadmin"); await p.fill("input[name=password]", "farm-e2e");
  await Promise.all([p.waitForNavigation(), p.click("button")]);
  await p.goto(GW + "/comfyfarm/dashboard/#/admin/rollout");
  await p.waitForFunction(() => document.querySelectorAll("#page .pill.s-match").length >= 2, null, { timeout: 20000 });
  check("instances show their badge", true);
  await p.screenshot({ path: `${OUT}/ui-rollout-before.png`, fullPage: true });

  // a good rollout
  check("rollout started from the dialog", (await startRollout(p, "next")) === "next");
  const ok = await waitRollout(p);
  check("rollout succeeded", ok.status === "succeeded" && ok.steps.every((s) => s.status === "done"), JSON.stringify(ok.steps.map((s) => s.status)));
  const inst = await getJson(p, "/api/admin/instances");
  const demo = inst.filter((i) => i.instance);
  check("every worker on the new profile with a matching badge", demo.every((i) => i.profile === "next" && i.badge === "match"),
        JSON.stringify(demo.map((i) => [i.worker, i.profile, i.badge])));
  await p.waitForTimeout(2500);
  await p.screenshot({ path: `${OUT}/ui-rollout-done.png`, fullPage: true });

  // in-use profiles are immutable
  const nextRow = p.locator("#page section").nth(2).locator("tbody tr").filter({ has: p.locator("td:first-child", { hasText: /^next$/ }) });
  await nextRow.locator("button", { hasText: "Edit" }).click();
  await p.locator("dialog.form button[type=submit]").click();
  await p.waitForSelector("#toast.bad:not([hidden])", { timeout: 5000 });
  check("editing an in-use profile is refused", (await p.textContent("#toast")).includes("copy it"), await p.textContent("#toast"));
  errors.splice(0, errors.length, ...errors.filter((e) => !e.includes("409")));   // that refusal was the point

  // a rollout the launcher refuses: the first worker is reverted, the rest skipped
  await p.waitForTimeout(800);
  check("second rollout started", (await startRollout(p, "next-broken")) === "next-broken");
  const bad = await waitRollout(p);
  const [s1, s2] = bad.steps;
  check("failed rollout: first worker reverted, rest skipped", bad.status === "failed" && s1.status === "reverted" && s2.status === "skipped",
        JSON.stringify(bad.steps.map((s) => [s.status, s.error])));
  check("the reason is the launcher's refusal", (s1.error || "").includes("refused"), s1.error);
  const after = await getJson(p, "/api/admin/instances");
  const w1 = after.find((i) => i.worker === s1.worker);
  check("reverted worker runs the previous profile", w1.profile === "next" && w1.badge === "match", JSON.stringify([w1.profile, w1.badge]));
  await p.waitForTimeout(2500);
  await p.screenshot({ path: `${OUT}/ui-rollout-failed.png`, fullPage: true });

  const audit = await getJson(p, "/api/admin/audit?limit=100");
  check("audit log records the assignments", audit.filter((e) => e.action === "assign").length >= 4);
  check("no page errors", errors.length === 0, errors.join(" | "));
  await b.close();
  console.log(failed ? `${failed} FAILED` : "ALL PASSED");
  process.exit(failed ? 1 : 0);
})();
