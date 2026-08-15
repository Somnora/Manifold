"use client";

import Link from "next/link";
import { api } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";

// Ambient signal that an AI agent is driving Manifold over MCP. The MCP
// bridge is a stateless HTTP client, so there is no live "connection" to
// display; what IS knowable and honest is recent activity, and every MCP
// tool call already lands in the audit log with actor "mcp". Recent call =
// an agent is working. Click-through lands on Activity, where each call
// and its note are listed.
//
// The one state that must never be silent is NEVER-CONNECTED (Phase 88):
// a user told their agent "Manifold is open for you to use" while no MCP
// call had ever reached this backend, and neither side had anywhere to
// see that. Zero all-time audit rows is a knowable fact, so it gets a
// chip - linking to the Settings card with the one-line connect command.
const ACTIVE_SECONDS = 5 * 60;      // teal: an agent is working right now
const RECENT_SECONDS = 60 * 60;     // grey: worked within the hour

function age(iso: string): number {
  return (Date.now() - new Date(iso).getTime()) / 1000;
}

function agoLabel(seconds: number): string {
  if (seconds < 90) return "now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
}

export function McpChip() {
  const { data: entries } = usePolling(() => api.audit("mcp", 1), 10000);
  if (!entries) return null;          // first poll still in flight
  const last = entries[0];
  if (!last) {
    // Never connected: say so where the user is looking, with the fix
    // one click away. (Once an agent HAS connected, quiet periods go back
    // to showing nothing - staleness is noise, absence is a trap.)
    return (
      <Link
        href="/settings#connect-agent"
        title="No MCP call has ever reached this backend. Click for the one-line command that connects Claude Code (or any MCP client)."
        className="flex h-8 items-center gap-1.5 rounded border border-dashed border-zinc-300 px-2.5 font-mono text-xs text-zinc-400 transition-colors hover:border-zinc-400 hover:text-zinc-600"
      >
        <span className="h-1.5 w-1.5 rounded-full bg-zinc-300" />
        no agent connected
      </Link>
    );
  }

  const seconds = age(last.at);
  if (seconds > RECENT_SECONDS) return null;   // stale: no chip, no noise
  const active = seconds <= ACTIVE_SECONDS;

  return (
    <Link
      href="/history"
      title={`Last MCP call: ${last.action} (${new Date(last.at).toLocaleTimeString()}). An agent drives Manifold through the MCP tools; every call is audited.`}
      className={`flex h-8 items-center gap-1.5 rounded border px-2.5 font-mono text-xs transition-colors ${
        active
          ? "border-teal-300/60 bg-teal-50 text-teal-800 hover:border-teal-400"
          : "border-zinc-200 text-zinc-400 hover:border-zinc-300 hover:text-zinc-600"
      }`}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${
          active ? "animate-pulse bg-teal-500" : "bg-zinc-400"
        }`}
      />
      MCP {agoLabel(seconds)}
    </Link>
  );
}
