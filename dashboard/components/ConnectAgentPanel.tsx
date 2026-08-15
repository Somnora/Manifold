"use client";

import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";

// "Manifold is open" and "Manifold is connected to this agent session" are
// different states, and Phase 88 started with a lost session where both
// sides believed the wrong one. This card is the dashboard's answer: the
// live fact (has any MCP call EVER reached this backend?) next to the
// exact one-line command that closes the gap - with --scope user, because
// the default local scope registers only for sessions started in one
// directory and reads as "not installed" from everywhere else.
export function ConnectAgentPanel({ envPath }: { envPath?: string }) {
  const { data: entries } = usePolling(() => api.audit("mcp", 1), 15000);
  const last = entries?.[0];

  // DATA_ROOT/.env parent is the install root: the repo in a dev checkout,
  // Application Support for the packaged app (whose command is the fixed
  // bundle path, not this).
  const devRoot =
    envPath && !envPath.includes("Application Support")
      ? envPath.replace(/\/\.env$/, "")
      : "<path-to-Manifold-repo>";
  const appCommand =
    'claude mcp add manifold --scope user -- "/Applications/Manifold.app/Contents/MacOS/manifold-backend" --mcp';
  const devCommand = `claude mcp add manifold --scope user -- uv run --directory "${devRoot}/backend" manifold-mcp`;

  return (
    <section
      id="connect-agent"
      className="rounded-lg border border-zinc-200 bg-white p-4"
    >
      <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
        Connect an agent (MCP)
      </h2>

      {entries &&
        (last ? (
          <p className="mt-2 text-xs text-zinc-500">
            Last MCP call:{" "}
            <span className="font-mono">{last.action}</span> at{" "}
            {new Date(last.at).toLocaleString()} — every call is on the{" "}
            <Link
              href="/history?tab=audit"
              className="text-teal-600 hover:underline"
            >
              Activity page
            </Link>
            .
          </p>
        ) : (
          <p className="mt-2 rounded border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-xs text-amber-800">
            No MCP call has ever reached this backend: Manifold may be open,
            but no agent is connected to it. Run one of the commands below
            in a terminal, then start a new agent session.
          </p>
        ))}

      <div className="mt-3 space-y-3">
        <CommandLine label="Installed app (macOS)" command={appCommand} />
        <CommandLine label="Dev checkout" command={devCommand} />
      </div>

      <p className="mt-3 text-xs text-zinc-500">
        <span className="font-mono">--scope user</span> makes manifold
        visible to Claude Code sessions in <em>every</em> directory; without
        it, only sessions started where you ran the command can see it. The
        installed-app bridge reads the API token from the app&apos;s own
        .env automatically; a dev-checkout bridge needs{" "}
        <span className="font-mono">MANIFOLD_API_TOKEN</span> in the MCP
        config&apos;s env block — or better, mint the agent its own token
        below (API access). Claude Desktop, Codex, and Gemini CLI:{" "}
        <span className="font-mono">docs/mcp-setup.md</span>.
      </p>
      <p className="mt-2 text-xs text-zinc-400">
        Verify the wiring end to end:{" "}
        <span className="font-mono">manifold-backend --doctor</span> (or{" "}
        <span className="font-mono">uv run manifold-doctor</span> from{" "}
        <span className="font-mono">backend/</span>) — backend up, token
        accepted, registered where, at what scope.
      </p>
    </section>
  );
}

function CommandLine({ label, command }: { label: string; command: string }) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");

  // navigator.clipboard is undefined on a page served over plain HTTP from
  // anything but localhost, so this throws rather than rejecting: catch it
  // and say so, the way the file browser does.
  async function copy() {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Could not access the clipboard. Select the command and copy it.");
    }
  }

  return (
    <div>
      <p className="text-xs text-zinc-400">{label}</p>
      <div className="mt-1 flex items-start gap-2">
        <code className="flex-1 overflow-x-auto whitespace-nowrap rounded border border-zinc-200 bg-zinc-50 px-2.5 py-1.5 font-mono text-xs text-zinc-700">
          {command}
        </code>
        <button
          type="button"
          onClick={copy}
          className="shrink-0 rounded border border-zinc-300 px-2.5 py-1.5 text-xs text-zinc-600 hover:border-zinc-400 hover:text-zinc-900"
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      {error && <p className="mt-1 text-xs text-red-700">{error}</p>}
    </div>
  );
}
