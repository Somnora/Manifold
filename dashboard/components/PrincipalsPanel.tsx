"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type Principal } from "@/lib/api";
import { formatDate } from "@/lib/format";

// Named API tokens (Phase 79). Every launch, job, and audit row carries
// the name of the token that caused it, so give each caller its own:
// an agent, a script, a coworker's laptop. The token value appears
// exactly once, in the mint response; the backend keeps only a hash.
// Management is owner-token-only until roles arrive (Phase 80).
export function PrincipalsPanel() {
  const [principals, setPrincipals] = useState<Principal[]>([]);
  const [authEnabled, setAuthEnabled] = useState<boolean | null>(null);
  const [name, setName] = useState("");
  const [minted, setMinted] = useState<{ name: string; token: string } | null>(
    null,
  );
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  function load() {
    api
      .principals()
      .then((r) => {
        setPrincipals(r.principals);
        setAuthEnabled(r.auth_enabled);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }
  useEffect(load, []);

  async function mint() {
    setBusy(true);
    setError("");
    setMinted(null);
    try {
      const r = await api.createPrincipal(name.trim().toLowerCase());
      setMinted({ name: r.name, token: r.token });
      setCopied(false);
      setName("");
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(principalName: string) {
    if (
      !window.confirm(
        `Revoke '${principalName}'? Every client using its token stops ` +
          `working immediately. Its name stays in history.`,
      )
    )
      return;
    setError("");
    try {
      await api.revokePrincipal(principalName);
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-5">
      <h3 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
        API access
      </h3>
      <p className="mt-1 text-xs text-zinc-500">
        Named tokens for other callers: agents, scripts, teammates. Whatever
        a token does is recorded under its name. Managing these needs the
        owner token (the one in .env).
      </p>

      {authEnabled === false && (
        <p className="mt-3 rounded border border-zinc-200 bg-zinc-50 px-3 py-2 text-xs text-zinc-500">
          API auth is off (no MANIFOLD_API_TOKEN configured), so named
          tokens have nothing to authenticate against. In mock mode this is
          normal.
        </p>
      )}

      {authEnabled && (
        <div className="mt-3 flex gap-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. claude-mcp, ci-runner"
            className="w-56 rounded border border-zinc-300 px-2.5 py-1.5 text-sm"
          />
          <button
            onClick={mint}
            disabled={busy || name.trim().length < 2}
            className="rounded border border-zinc-900 bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
          >
            {busy ? "Minting..." : "Mint token"}
          </button>
        </div>
      )}

      {minted && (
        <div className="mt-3 rounded border border-amber-300 bg-amber-50 p-3">
          <p className="text-xs font-medium text-amber-900">
            Token for &apos;{minted.name}&apos;. This is the only time it is
            shown; the backend keeps a hash, not the value.
          </p>
          <div className="mt-2 flex items-center gap-2">
            <code className="break-all rounded bg-white px-2 py-1 font-mono text-xs text-zinc-800">
              {minted.token}
            </code>
            <button
              onClick={() =>
                navigator.clipboard
                  .writeText(minted.token)
                  .then(() => setCopied(true))
              }
              className="shrink-0 rounded border border-amber-400 px-2 py-1 text-xs text-amber-900 hover:bg-amber-100"
            >
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
        </div>
      )}

      {principals.length > 0 && (
        <ul className="mt-4 divide-y divide-zinc-100">
          {principals.map((p) => (
            <li key={p.id} className="flex items-center gap-3 py-2 text-sm">
              <span className="font-mono text-zinc-800">{p.name}</span>
              {p.revoked_at ? (
                <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-[11px] text-zinc-500">
                  revoked {formatDate(p.revoked_at)}
                </span>
              ) : (
                <span className="text-xs text-zinc-400">
                  {p.last_used_at
                    ? `last used ${formatDate(p.last_used_at)}`
                    : "never used"}
                </span>
              )}
              <span className="ml-auto text-[11px] text-zinc-400">
                minted by {p.created_by}
              </span>
              {!p.revoked_at && (
                <button
                  onClick={() => revoke(p.name)}
                  className="rounded border border-zinc-200 px-2 py-0.5 text-xs text-zinc-500 hover:bg-red-50 hover:text-red-600"
                >
                  Revoke
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {error && <p className="mt-3 text-xs text-red-700">{error}</p>}
    </div>
  );
}
