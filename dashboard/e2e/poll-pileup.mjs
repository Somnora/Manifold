// The freeze, reproduced and then measured away.
//
//   MANIFOLD_MOCK=1 MANIFOLD_DATA_DIR=$(mktemp -d) \
//     uv run uvicorn app.main:create_default_app --factory --port 8033
//   NEXT_PUBLIC_API_URL=http://localhost:8033 npm run build
//   python3 -m http.server 3000 --directory out
//   SITE=http://127.0.0.1:3000 node e2e/poll-pileup.mjs
//
// No API variable: the backend's address is discovered from the page's own
// traffic (see below). Set API= only to override that.
//
// Port 3000 exactly: the backend's CORS allowlist is localhost:3000 and
// nothing else, and every api call is blocked at the preflight anywhere else.
// playwright must be resolvable by node; `npx playwright node file.mjs` is not
// a thing. (Both traps cost a debugging round each on agent-connection.mjs.)
//
// WHAT THIS CATCHES. usePolling ran `setInterval(tick, intervalMs)` with no
// guard, and lib/api.ts waits 30 SECONDS before it gives up on a request. So
// when the backend went slow, every hook queued a new fetch every interval
// while none of them finished: arrivals outran completions and the queue grew
// without bound. A browser opens ~6 connections per origin, so the overflow
// waited in its network queue - and the fetch behind a click on a tab waited
// there too, behind fifty polls. That is the "app froze after I stepped away,
// I have to restart it" report, and it is self-sustaining: the pile-up is
// what makes the next request slow.
//
// The stall below is what a laptop resuming from sleep does to a backend
// whose SSH connections went stale. Nothing here fakes the frontend: real
// timers, real fetches, real browser connection limits.
//
// The visibility half IS stubbed, and deliberately: headless Chromium will
// not reliably background a tab on request, so document.hidden is overridden
// and a visibilitychange event dispatched. That exercises this repo's code in
// a real browser with real timers; it does not re-test Chrome's own event.
import { chromium } from "playwright";

const SITE = process.env.SITE || "http://127.0.0.1:3000";
const STALL = Number(process.env.STALL || 8000);   // every API call takes this
const WINDOW = Number(process.env.WINDOW || 24000);
// ".html", not "/jobs": a static export served by a plain file server has no
// directory rewrite, so /jobs/ is a DIRECTORY LISTING - a 200 with no app on
// it, no hooks mounted, and every assertion below passing against zero
// traffic. Vacuously green is the failure mode this file must not have.
const PAGE = process.env.PAGE || "/jobs.html";     // the most-polled page

const fail = [];
const ok = (c, m) => { console.log(`${c ? "PASS" : "FAIL"}  ${m}`); if (!c) fail.push(m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1440, height: 1000 } });

// document.hidden is a getter on the prototype; override it before any app
// code runs so the hook installed by usePolling sees our value.
await p.addInitScript(() => {
  let hidden = false;
  Object.defineProperty(Document.prototype, "hidden", { get: () => hidden });
  Object.defineProperty(Document.prototype, "visibilityState", {
    get: () => (hidden ? "hidden" : "visible"),
  });
  window.__setHidden = (v) => {
    hidden = v;
    document.dispatchEvent(new Event("visibilitychange"));
  };
});

// WHERE the backend is, discovered rather than declared. lib/backend.ts
// bakes the base in at build time and defaults to `http://localhost:8000`,
// which is NOT `http://127.0.0.1:8000` to a URL prefix. Being handed the
// wrong spelling matched zero requests, stalled nothing, and passed every
// upper bound in this file - CI caught it on the preconditions, which is
// exactly what they are for. So the page is watched instead of trusted: the
// first origin it calls that is not the site IS the backend.
let API = null;
const siteOrigin = new URL(SITE).origin;

// Accounting. `request` fires when the page initiates a fetch, and finished/
// failed when it is done, so inflight is what the browser is actually holding
// open - the number the connection limit applies to.
let inflight = 0, peak = 0, started = 0;
const perUrl = new Map();
const inflightPerUrl = new Map();
let counting = false;
const isApi = (u) => API !== null && u.startsWith(API);
const key = (u) => u.slice(API.length).split("?")[0];

p.on("request", (r) => {
  if (!counting || !isApi(r.url())) return;
  started += 1;
  inflight += 1;
  if (inflight > peak) peak = inflight;
  const k = key(r.url());
  const n = (inflightPerUrl.get(k) || 0) + 1;
  inflightPerUrl.set(k, n);
  perUrl.set(k, Math.max(perUrl.get(k) || 0, n));
});
const done = (r) => {
  if (!counting || !isApi(r.url())) return;
  inflight -= 1;
  const k = key(r.url());
  inflightPerUrl.set(k, (inflightPerUrl.get(k) || 1) - 1);
};
p.on("requestfinished", done);
p.on("requestfailed", done);

// Every non-site origin the page calls, so the backend can be identified
// from its own traffic. Registered before navigation so nothing is missed.
const foreign = new Set();
p.on("request", (r) => {
  const o = new URL(r.url()).origin;
  if (o !== siteOrigin) foreign.add(o);
});

// The slow backend. Held before continue(), so from the page's side the
// fetch is simply pending - exactly a backend that has not answered yet.
// The predicate reads API on every request, so routing simply does nothing
// until the origin below is discovered.
let stalling = false;
await p.route((u) => API !== null && u.href.startsWith(API), async (route) => {
  if (stalling) await sleep(STALL);
  await route.continue();
});

await p.goto(`${SITE}${PAGE}`, { waitUntil: "domcontentloaded" });
await sleep(3000);                     // let the page mount its hooks

API = process.env.API || [...foreign][0] || null;
// Every origin, not just the chosen one: when this picks wrong, the next
// line is the whole diagnosis instead of another round trip through CI.
console.log(`  off-site origins seen: ${[...foreign].join(", ") || "(none)"}`);
console.log(`  measuring: ${API ?? "(nothing off-site)"}`
  + (process.env.API ? "  [API= override in effect]" : ""));
if (!API) {
  ok(false, "the page never called a backend - nothing to measure");
  await b.close();
  process.exit(1);
}
await sleep(1500);                     // let routing settle onto the origin

// -- 1. a stalled backend must not produce an unbounded queue ----------------
console.log(`  stalling every API call by ${STALL}ms for ${WINDOW}ms...`);
counting = true;
stalling = true;
await sleep(WINDOW);
// The backlog the moment the backend recovers: what a returning user's click
// would have had to queue behind. Sampled here rather than after a fixed
// wait, which measured how the timers happened to line up.
const backlog = inflight;
stalling = false;

const hooks = perUrl.size;
console.log(`  ${started} requests, peak ${peak} in flight, ${hooks} distinct endpoints`);
// FIRST, before any ceiling is checked: every assertion below is an upper
// bound, and upper bounds are all satisfied by measuring nothing at all. The
// first run of this file did exactly that (wrong URL, no app, four green
// checks), so the precondition is asserted rather than assumed.
ok(started > 5, `precondition: the page is really polling (${started} calls)`);
ok(hooks >= 4, `precondition: several endpoints are live (${hooks})`);
console.log("  peak concurrency per endpoint:");
for (const [k, n] of [...perUrl.entries()].sort((a, b2) => b2[1] - a[1])) {
  console.log(`    ${String(n).padStart(3)}  ${k}`);
}

// The invariant: ONE poll in flight per hook. Endpoints polled by more than
// one component legitimately exceed 1, so the ceiling is stated per endpoint
// rather than absolutely.
//
// Every bound here is set from a measured pair, not from taste. Same page,
// same window, same stall, run against the usePolling that shipped in v0.2.3
// and against the one that replaced it:
//
//                              before   after
//   requests over the window     27      13
//   peak in flight               12       5
//   worst single endpoint         5       2
//   backlog when it recovered    10       5
//   issued while hidden (12s)    13       0
//   reloads within 1.2s of reveal 0       7
//
// Six of the nine checks below fail on the old code. They discriminate with
// room on both sides; loosening one without re-running that pair turns this
// file back into decoration.
const worst = Math.max(0, ...perUrl.values());
ok(worst <= 4, `no endpoint exceeds 4 concurrent polls (worst: ${worst})`);
ok(peak <= 2 * hooks, `total in flight stays bounded (peak ${peak}, ${hooks} endpoints)`);

// A hook that finishes cannot have issued more than one poll per stall, so
// with the guard the count is governed by STALL, not by the interval.
const ceiling = Math.ceil(WINDOW / STALL) + 1;
ok(
  started <= hooks * ceiling,
  `each endpoint polled at most ~${ceiling}x during the stall `
  + `(${started} total across ${hooks} endpoints)`,
);

// -- 2. the backlog a returning user would queue behind ----------------------
// This is the freeze itself, as a number. A browser opens ~6 connections per
// origin, so once the backlog passes that, a click's fetch does not go out -
// it waits for a poll to finish first.
// 6 is not a taste: it is how many connections a browser opens per origin
// over HTTP/1.1, and uvicorn speaks HTTP/1.1. At or below it, a click's
// fetch goes out immediately. Above it, the click waits for a POLL to
// finish, which is the entire user-visible symptom.
//
// This is therefore a budget for the PAGE, not only for the hook: adding
// enough concurrent polls to exceed the connection limit would fail here
// even with the guard working, and that failure would be correct.
console.log(`  ${backlog} requests outstanding when the backend recovered`);
ok(backlog <= 6, `backlog at recovery fits the browser's connection limit (${backlog})`);

// And it must actually drain. A generous deadline: a request already asleep
// in the stall keeps its full delay, so this is a leak check, not a race.
const deadline = Date.now() + STALL + 8000;
while (inflight > 0 && Date.now() < deadline) await sleep(250);
ok(inflight === 0, `every request drained after recovery (${inflight} left)`);

// -- 3. a hidden window does not poll ----------------------------------------
// Stepping away is when the pile-up was built, and coalesced background
// timers all fire at once on wake.
counting = false;
await sleep(1500);
await p.evaluate(() => window.__setHidden(true));
await sleep(500);
const before = started;
counting = true;
await sleep(12000);                    // several intervals of every hook
const whileHidden = started - before;
console.log(`  ${whileHidden} requests issued while hidden`);
ok(whileHidden === 0, "a hidden document issues no polls");

// -- 4. and coming back reloads immediately ----------------------------------
const atReveal = started;
await p.evaluate(() => window.__setHidden(false));
await sleep(1200);                     // well under the shortest interval (2s)
const onReveal = started - atReveal;
console.log(`  ${onReveal} requests issued within 1.2s of becoming visible`);
ok(onReveal > 0, "becoming visible reloads at once rather than waiting an interval");

await b.close();
console.log(fail.length ? `\n${fail.length} FAILED` : "\nall checks passed");
process.exit(fail.length ? 1 : 0);
