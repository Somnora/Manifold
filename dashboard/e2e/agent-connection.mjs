// "No agent connected", measured in a real browser.
//
//   # a backend whose audit log has NEVER seen an mcp call
//   MANIFOLD_MOCK=1 MANIFOLD_DATA_DIR=$(mktemp -d) \
//     uv run uvicorn app.main:create_default_app --factory --port 8022
//   NEXT_PUBLIC_API_URL=http://127.0.0.1:8022 npm run build
//   python3 -m http.server 3000 --directory out
//   API=http://127.0.0.1:8022 SITE=http://127.0.0.1:3000 node e2e/agent-connection.mjs
//
// Port 3000 exactly, even though NEXT_PUBLIC_API_URL points the export at
// another port: the backend's CORS allowlist is localhost:3000 / 127.0.0.1:3000
// and nothing else. Served anywhere else, EVERY api call is blocked at the
// preflight, `entries` stays null, and the chip renders nothing - which looks
// exactly like the bug this file exists to catch. It cost a debugging round.
//
// The export is built with NEXT_PUBLIC_API_URL rather than pointed at :8000,
// because this check needs a backend with an EMPTY audit log and the
// developer's own backend never has one.
//
// playwright must be resolvable by node (npm i playwright@1.62 in a scratch
// dir and run from there, or add it to the dashboard's devDependencies);
// `npx playwright node file.mjs` does NOT work - playwright's CLI has no
// `node` subcommand.
//
// Why this file exists: the never-connected state is the one state a real
// backend can never show you. The owner's has 251 mcp rows going back a
// month, so every manual look at this UI shows the connected path, and the
// empty path shipped in v0.2.2 verified by nothing but a typecheck. The
// incident that produced it (an agent told "Manifold is open for you to
// use" that could not find Manifold, while a GPU billed) is exactly what
// this chip exists to prevent, so it gets a check that runs without a
// human noticing the difference.
import { chromium } from "playwright";

const API = process.env.API || "http://127.0.0.1:8022";
const SITE = process.env.SITE || "http://127.0.0.1:3000";
const fail = [];
const ok = (c, m) => { console.log(`${c ? "PASS" : "FAIL"}  ${m}`); if (!c) fail.push(m); };

const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1440, height: 1000 } });
p.on("pageerror", (e) => { ok(false, `page error: ${e.message}`); });

// The backend's own truth first: the UI is measured against DATA, never
// against a number typed in by the author.
const seen = await (await fetch(`${API}/audit?actor=mcp&limit=5`)).json();
console.log(`  backend reports ${seen.entries.length} mcp audit rows`);
ok(seen.entries.length === 0, "precondition: backend has never seen an mcp call");

// -- 1. the header chip states the never-connected fact -----------------------
await p.goto(`${SITE}/`, { waitUntil: "networkidle" });
await p.waitForTimeout(1500);

const chip = p.getByRole("link", { name: /no agent connected/i });
ok(await chip.isVisible(), "header shows a 'no agent connected' chip");
ok(
  (await chip.getAttribute("href") || "").includes("/settings"),
  "the chip links to Settings, where the fix lives",
);
// The old bug in one assertion: this state used to render nothing at all.
ok(
  (await p.locator("header a, header span").count()) > 0,
  "header is not silent in the never-connected state",
);

// -- 2. clicking it lands on a card that says what to run --------------------
await chip.click();
await p.waitForLoadState("networkidle");
await p.waitForTimeout(1200);
ok(p.url().includes("/settings"), "the chip navigates to Settings");

const panel = p.locator("#connect-agent");
ok(await panel.isVisible(), "Settings has a 'Connect an agent' card");
const panelText = await panel.innerText();
ok(
  /no mcp call has ever reached this backend/i.test(panelText),
  "the card states the never-connected fact in words",
);
ok(
  panelText.includes("--scope user"),
  "the card's command carries --scope user (the flag whose absence caused the incident)",
);
ok(
  panelText.includes("claude mcp add manifold"),
  "the card gives the actual registration command",
);
ok(/--doctor/.test(panelText), "the card points at --doctor for verification");

// A command the user is told to copy must be copyable.
const copyButtons = panel.getByRole("button", { name: /copy/i });
ok(await copyButtons.count() >= 1, "the commands have Copy buttons");

// -- 3. the state FLIPS once an agent actually calls -------------------------
// Proves the chip reports live audit truth rather than a static empty state.
await fetch(`${API}/audit/agent`, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({
    tool: "list_instances", args: {}, note: "e2e: first contact", result: "ok",
  }),
});
await p.goto(`${SITE}/`, { waitUntil: "networkidle" });
await p.waitForTimeout(1500);
ok(
  !(await p.getByRole("link", { name: /no agent connected/i }).isVisible().catch(() => false)),
  "after the first mcp call, the never-connected chip is gone",
);
ok(
  await p.getByRole("link", { name: /MCP\s+now/i }).isVisible().catch(() => false),
  "and the active 'MCP now' chip has taken its place",
);

await b.close();
console.log(fail.length ? `\n${fail.length} FAILED` : "\nall checks passed");
process.exit(fail.length ? 1 : 0);
