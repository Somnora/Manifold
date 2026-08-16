// A shell that outlives its socket, proven from the browser side.
//
//   MANIFOLD_MOCK=1 MANIFOLD_DATA_DIR=$(mktemp -d) \
//     uv run uvicorn app.main:create_default_app --factory --port 8033
//   NEXT_PUBLIC_API_URL=http://127.0.0.1:8033 npm run build
//   python3 -m http.server 3000 --directory out
//   SITE=http://127.0.0.1:3000 node e2e/terminal-reconnect.mjs
//
// Port 3000 exactly (the backend's WS origin allowlist is localhost only),
// and playwright must be resolvable by node. See poll-pileup.mjs.
//
// WHAT THIS CATCHES. Phase 91 taught the backend to park a shell for 8 hours
// after its socket dropped, so a frozen tab would stop costing an agent
// session. Nothing ever went back for it: onclose set status "closed" and
// that was the end of the panel's involvement. The grace window was real and
// entirely invisible - the user still saw a dead rectangle, still killed the
// tab, and still lost the conversation the parked shell was holding.
//
// TWO DELIBERATE CHOICES, both about honesty rather than convenience:
//
// ?renderer=dom, because the WebGL renderer draws glyphs on a canvas and
// leaves nothing to read. The escape hatch already existed for diagnosis.
// What is asserted is the SHELL's output, which is renderer-independent.
//
// The socket is closed from the page rather than by unplugging anything.
// Playwright's offline switch does not reach loopback (measured: the socket
// stayed open and every assertion here passed vacuously), and a laptop that
// sleeps sends no close frame either. From both the panel's side and the
// backend's, a socket that stops is a socket that stops. Chrome's own
// network detection is not this repo's code and is not retested here.
//
// The last section stubs nothing at all: `exit` really ends the shell, the
// backend really answers 4410, and the panel really has to stay closed.
import { chromium } from "playwright";

const SITE = process.env.SITE || "http://127.0.0.1:3000";
const fail = [];
const ok = (c, m) => { console.log(`${c ? "PASS" : "FAIL"}  ${m}`); if (!c) fail.push(m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const count = (hay, needle) => hay.split(needle).length - 1;

const b = await chromium.launch();
const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 } });
const p = await ctx.newPage();
p.on("pageerror", (e) => ok(false, `page error: ${e.message}`));

// Every socket the page opens, so the reconnect can be observed as a fact
// (a SECOND socket exists) rather than inferred from a status word.
await p.addInitScript(() => {
  const Real = window.WebSocket;
  window.__sockets = [];
  window.WebSocket = function (...a) {
    const s = new Real(...a);
    window.__sockets.push(s);
    return s;
  };
  window.WebSocket.prototype = Real.prototype;
  Object.assign(window.WebSocket, Real);
});

const statusOf = async () => (
  await p.locator("text=/^(open|closed|connecting|reconnecting)$/")
    .first().innerText().catch(() => "")
).trim();
const waitForStatus = async (want, ms) => {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (await statusOf() === want) return true;
    await sleep(150);
  }
  return false;
};
const screen = async () => (
  await p.locator(".xterm-rows").first().innerText().catch(() => "")
).replace(/\s+/g, " ");
const type = async (s) => {
  // xterm listens on a hidden textarea; clicking the canvas is not enough.
  await p.locator("textarea.xterm-helper-textarea").first().focus();
  await p.keyboard.type(s);
};
const socketCount = () => p.evaluate(() => window.__sockets.length);

await p.goto(`${SITE}/?renderer=dom`, { waitUntil: "domcontentloaded" });
await sleep(2500);

// -- 1. a local shell, running -----------------------------------------------
await p.getByRole("button", { name: /open local terminal/i }).click();
ok(await waitForStatus("open", 20000), "the local shell connects");
ok(await socketCount() === 1, "exactly one socket so far");

// A marker only this run could have produced, so its survival below cannot be
// a coincidence, a cached frame, or another run's shell.
const marker = `mfld-${process.pid}-${process.hrtime.bigint() % 100000n}`;
await type(`echo ${marker}\n`);
await sleep(2500);
const before = await screen();
ok(before.includes(marker), `the shell ran a command (${marker})`);
const echoes = count(before, marker);      // the typed line + its output

// -- 2. the socket stops, the way a sleeping laptop stops it -----------------
await p.evaluate(() => window.__sockets[window.__sockets.length - 1].close());

// The old behavior in one assertion. Sampled continuously rather than once,
// because the recovery is fast and a single late look would miss a "closed"
// that flashed - and a panel that declares the shell dead has already told
// the user to kill the tab, however quickly it changes its mind after.
const seen = new Set();
const until = Date.now() + 4000;
while (Date.now() < until) {
  seen.add(await statusOf());
  await sleep(100);
}
ok(!seen.has("closed"), `never reports the shell closed (saw: ${[...seen].join(", ")})`);

// -- 3. it goes back for the shell on its own --------------------------------
ok(await waitForStatus("open", 30000), "it reattaches without a refresh");
ok(await socketCount() >= 2, "a second socket was opened by the panel itself");

// -- 4. and it is the SAME shell, exactly once -------------------------------
// A reconnect that started a fresh shell looks identical in the status line
// and loses everything the user cared about. A reconnect that forgot to
// reset the terminal paints the whole session a second time.
const after = await screen();
ok(after.includes(marker), "the scrollback from before the drop is still there");
ok(
  count(after, marker) === echoes,
  `the replay did not duplicate the session (${echoes} before, ${count(after, marker)} after)`,
);
const live = `${marker}-live`;
await type(`echo ${live}\n`);
await sleep(2500);
ok((await screen()).includes(live), "and the reattached shell still takes input");

// -- 5. a shell that really ended stays ended --------------------------------
// Nothing stubbed: the backend closes with 4410 and the panel must not come
// back. Getting this wrong would resurrect a shell on every `exit`, and two
// tabs on one session would trade it forever.
const beforeExit = await socketCount();
await type("exit\n");
ok(await waitForStatus("closed", 20000), "an exited shell reports closed");
await sleep(6000);                          // several backoffs' worth
ok(await statusOf() === "closed", "and stays closed rather than reconnecting");
ok(
  await socketCount() === beforeExit,
  "no socket was opened after the shell ended",
);

await b.close();
console.log(fail.length ? `\n${fail.length} FAILED` : "\nall checks passed");
process.exit(fail.length ? 1 : 0);
