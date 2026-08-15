// Does the Launch Swarm dialog actually escape the cluster panel?
//
//   npm run build && npx serve out -l 3111   (or any static server)
//   npx --yes playwright@1.62 node e2e/modal-portal.mjs
//
// Run against the STATIC EXPORT, not `next dev`: dev blocks cross-origin
// asset requests from 127.0.0.1, the bundles 403, React never hydrates and
// every click silently does nothing - which looks exactly like a broken
// fix. BASE overrides the URL.
//
// This is the only browser-level check in the repo, and it exists because
// `npm run build` and `tsc` both pass happily while a dialog is invisible:
// the bug was pure CSS containing-block behaviour. Verified to FAIL on the
// pre-fix component (5 assertions, including the dialog sliding 140px on
// scroll) - a check that cannot fail proves nothing.
//
// The bug: the panel is `backdrop-blur-md overflow-hidden`, which made it
// the containing block for a `position: fixed` child AND clipped it. The
// assertions below are the three symptoms from the screen recording:
//   1. the dialog is not a DOM descendant of the panel
//   2. its rendered box is fully inside the viewport (not a clipped sliver)
//   3. it does not move when the page scrolls
import { chromium } from "playwright";

const BASE = process.env.BASE || "http://127.0.0.1:3000";
const fail = [];
const ok = (cond, msg) => { console.log(`${cond ? "PASS" : "FAIL"}  ${msg}`); if (!cond) fail.push(msg); };

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
page.on("pageerror", (e) => console.log("  [page error]", e.message));

await page.goto(BASE, { waitUntil: "networkidle" });

// The panel is the ancestor that used to trap the dialog.
const panel = page.locator("div").filter({ hasText: "Elastic GPU Clusters" }).last();
await panel.waitFor({ timeout: 20000 });

const trap = await page.evaluate(() => {
  const h2 = [...document.querySelectorAll("h2")].find((e) =>
    e.textContent.includes("Elastic GPU Clusters"));
  const card = h2?.closest("div.relative");
  const cs = card ? getComputedStyle(card) : null;
  return cs ? { backdrop: cs.backdropFilter, overflow: cs.overflow } : null;
});
console.log("  panel styles (the trap):", JSON.stringify(trap));
ok(trap && trap.backdrop !== "none",
   "precondition: the panel really does have a backdrop-filter (the trap is real)");

await page.getByRole("button", { name: /Launch Swarm/i }).first().click();
// Target the dialog CARD by its content, not by role= - the buggy version
// had no role attribute, and a check that can only fail on a missing
// attribute proves nothing about the layout bug it is meant to catch.
const dialog = page.locator("div.rounded-2xl").filter({
  has: page.getByRole("heading", { name: "Launch Swarm", exact: true }),
}).first();
await dialog.waitFor({ state: "visible", timeout: 10000 });
// The dialog animates in (framer-motion: y 10 -> 0, scale 0.95 -> 1). Let
// it settle, or "before scroll" is measured mid-flight and the scroll
// assertion reads the animation's tail as movement.
await page.waitForFunction(() => {
  const h3 = [...document.querySelectorAll("h3")].find(
    (e) => e.textContent.trim() === "Launch Swarm");
  const d = h3?.closest("div.rounded-2xl");
  if (!d) return false;
  const y = d.getBoundingClientRect().top;
  if (window.__lastY === y) return true;      // two identical frames = settled
  window.__lastY = y;
  return false;
}, { timeout: 5000, polling: 120 });

// 1. Not a descendant of the panel.
const escaped = await page.evaluate(() => {
  const h3 = [...document.querySelectorAll("h3")].find(
    (e) => e.textContent.trim() === "Launch Swarm");
  const d = h3?.closest("div.rounded-2xl");
  const h2 = [...document.querySelectorAll("h2")].find((e) =>
    e.textContent.includes("Elastic GPU Clusters"));
  const card = h2?.closest("div.relative");
  // portalled = the dialog's chain reaches <body> without passing through
  // the panel.
  return {
    inPanel: !!(card && card.contains(d)),
    parentIsBody: d?.parentElement?.parentElement === document.body
                  || d?.parentElement === document.body,
  };
});
ok(!escaped.inPanel, "dialog is NOT inside the cluster panel");
ok(escaped.parentIsBody, "dialog is portalled to <body>");

// 2. Fully visible: the whole box within the viewport, and a real size.
const box = await dialog.boundingBox();
const vp = page.viewportSize();
console.log("  dialog box:", JSON.stringify(box));
ok(box.width > 300 && box.height > 200, "dialog has a real size (not a clipped sliver)");
ok(box.y >= -1 && box.y + box.height <= vp.height + 1,
   "dialog fits vertically inside the viewport");
ok(box.x >= -1 && box.x + box.width <= vp.width + 1,
   "dialog fits horizontally inside the viewport");

// 3. Scrolling the page must not move it (it is viewport-fixed now).
const before = await dialog.boundingBox();
await page.mouse.wheel(0, 600);
await page.waitForTimeout(400);
const after = await dialog.boundingBox();
console.log(`  dialog y before scroll ${before.y}, after ${after.y}`);
ok(Math.abs(before.y - after.y) < 2, "dialog does NOT scroll away with the page");

// Bonus: the page behind is locked, and Escape closes.
const locked = await page.evaluate(() => getComputedStyle(document.body).overflow);
ok(locked === "hidden", "page behind the dialog is scroll-locked");
await page.keyboard.press("Escape");
const closed = await dialog.waitFor({ state: "detached", timeout: 5000 })
  .then(() => true).catch(() => false);
ok(closed, "Escape closes the dialog");

await browser.close();
console.log(fail.length ? `\n${fail.length} FAILED` : "\nALL CHECKS PASSED");
process.exit(fail.length ? 1 : 0);
