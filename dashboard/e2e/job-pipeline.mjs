// The Job pipeline, measured in a real browser.
//
//   npm run build && python3 -m http.server 3000 --directory out
//   TOK=<MANIFOLD_API_TOKEN> npx --yes playwright@1.62 node e2e/job-pipeline.mjs
//
// Port 3000 specifically: below that the export talks to localhost:8000,
// on any other port it calls ITSELF for the API and renders no jobs.
// Needs a backend with tasks in it - mock mode is fine.
//
// Every assertion is a symptom from the 2026-08-15 report: 43 nodes with
// 41 independent produced a 6,150px single column inside a 400px canvas,
// which React Flow could not fit because its default minZoom is 0.5. The
// numbers below are geometry, not vibes - each one failed before the fix.
import { chromium } from "playwright";
const fail = [];
const ok = (c, m) => { console.log(`${c ? "PASS" : "FAIL"}  ${m}`); if (!c) fail.push(m); };

const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1440, height: 1000 } });
// The static export talks to the real backend on :8000, which enforces a
// token; seed it the way TokenGate does so the panel gets real data.
const tok = process.env.TOK;
await p.addInitScript((t) => localStorage.setItem("manifold-api-token:http://localhost:8000", t), tok);
await p.goto("http://localhost:3000/", { waitUntil: "networkidle" });
await p.waitForTimeout(4000);

// What the backend says exists - the check measures the UI against the
// DATA, not against a number hardcoded from the author's machine.
const api = process.env.API || "http://localhost:8000";
const expected = await p.evaluate(async (base) => {
  const r = await fetch(`${base}/tasks`);
  const tasks = (await r.json()).tasks ?? [];
  return {
    total: tasks.length,
    chained: tasks.filter((t) => (t.depends_on ?? []).length > 0).length,
  };
}, api);
console.log("  backend reports:", JSON.stringify(expected));
ok(expected.total > 0,
   "precondition: the backend has tasks to draw (seed some, else this " +
   "check silently passes on an empty graph)");

const canvas = p.locator(".react-flow").first();
await canvas.waitFor({ timeout: 20000 });
await canvas.scrollIntoViewIfNeeded();
await p.waitForTimeout(1200);   // let the re-fit animation settle
const box = await canvas.boundingBox();

const geom = await p.evaluate(() => {
  const nodes = [...document.querySelectorAll(".react-flow__node")];
  const vp = document.querySelector(".react-flow__viewport");
  const pane = document.querySelector(".react-flow__pane");
  const pr = pane.getBoundingClientRect();
  const rects = nodes.map((n) => n.getBoundingClientRect());
  const inside = rects.filter(
    (r) => r.top >= pr.top - 2 && r.bottom <= pr.bottom + 2 &&
           r.left >= pr.left - 2 && r.right <= pr.right + 2);
  const xs = new Set(nodes.map((n) => Math.round(parseFloat(n.style.transform.match(/translate\(([-\d.]+)px/)?.[1] ?? "0"))));
  return {
    count: nodes.length,
    inside: inside.length,
    distinctColumns: xs.size,
    transform: vp.style.transform,
    edges: document.querySelectorAll(".react-flow__edge").length,
    markers: document.querySelectorAll("marker").length,
    minimaps: document.querySelectorAll(".react-flow__minimap").length,
  };
});
console.log("  geometry:", JSON.stringify(geom));

ok(geom.count === expected.total,
   `every job rendered (${geom.count}/${expected.total})`);
ok(geom.inside === geom.count,
   `every node is inside the canvas (${geom.inside}/${geom.count}) - nothing clipped`);
// The grid only kicks in once there are enough independent jobs to wrap.
const isolated = expected.total - expected.chained;
if (isolated >= 4) {
  ok(geom.distinctColumns > 1,
     `independent jobs are gridded, not one column (${geom.distinctColumns} columns)`);
} else {
  console.log(`SKIP  grid check: only ${isolated} independent jobs`);
}
if (expected.chained > 0) {
  ok(geom.edges > 0, `dependency edges drawn (${geom.edges})`);
  ok(geom.markers > 0,
     "edges carry arrowheads (the label says 'an arrow means runs after')");
} else {
  console.log("SKIP  arrow checks: no chained jobs in this backend");
}

// The minimap must not be an opaque slab over the jobs.
const mm = await p.locator(".react-flow__minimap").first().boundingBox().catch(() => null);
if (mm) {
  const frac = (mm.width * mm.height) / (box.width * box.height);
  console.log(`  minimap covers ${(frac * 100).toFixed(1)}% of the canvas`);
  ok(frac < 0.10, "minimap is small enough not to cover the graph");
} else ok(true, "no minimap needed at this size");

await canvas.screenshot({ path: "/private/tmp/claude-501/pipeline-fixed.png" });
await b.close();
console.log(fail.length ? `\n${fail.length} FAILED` : "\nALL CHECKS PASSED");
process.exit(fail.length ? 1 : 0);
