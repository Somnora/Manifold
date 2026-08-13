"use client";

import { useEffect, useState } from "react";
import { setToken, UNAUTHORIZED_EVENT } from "@/lib/token";

// The way into a token-protected backend (Phase 78). Two jobs:
//
// 1. Bootstrap: the Tauri desktop shell reads MANIFOLD_API_TOKEN from the
//    app's .env and lands on /?token=<value>. Store it and scrub the URL
//    immediately so the token never sits in the address bar, browser
//    history, or a screenshot.
// 2. Recovery: whenever any request comes back 401 (no token stored, or a
//    rotated one), show a small paste gate. Saving reloads the page so
//    every poller restarts authenticated.
//
// Mounted at the layout level so any page can recover. It appears at most
// once per browser profile in normal use, so it stays calm and small.
export function TokenGate() {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const fromUrl = params.get("token");
    if (fromUrl) {
      setToken(fromUrl);
      params.delete("token");
      const qs = params.toString();
      window.history.replaceState(
        null,
        "",
        window.location.pathname + (qs ? `?${qs}` : "") + window.location.hash,
      );
    }
    const onUnauthorized = () => setOpen(true);
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  if (!open) return null;

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const token = value.trim();
    if (!token) return;
    setToken(token);
    // Reload rather than retry-in-place: every polling component picks up
    // the token on its next request with zero per-component wiring.
    window.location.reload();
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-zinc-900/40 p-6">
      <div className="w-full max-w-md rounded-lg border border-zinc-200 bg-white p-5 shadow-lg">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
          API token required
        </h2>
        <p className="mt-2 text-xs leading-relaxed text-zinc-600">
          This backend asks for its API token before serving anything. It is
          the <span className="font-mono">MANIFOLD_API_TOKEN</span> line in
          Manifold&apos;s .env file: the repo root in development, or the
          app&apos;s data folder for the desktop app (on macOS,{" "}
          <span className="font-mono">
            ~/Library/Application Support/Manifold
          </span>
          ). Paste it once; this browser remembers it.
        </p>
        <form onSubmit={submit} className="mt-3 flex gap-2">
          <input
            type="password"
            className="flex-1 rounded border border-zinc-300 bg-white px-2.5 py-1.5 font-mono text-sm"
            placeholder="paste the API token"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            autoComplete="off"
            autoFocus
          />
          <button
            type="submit"
            disabled={!value.trim()}
            className="rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
          >
            Unlock
          </button>
        </form>
      </div>
    </div>
  );
}
