"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type ResearchKey } from "@/lib/api";

// The research-key vault (Phase 100): third-party API keys for research
// work (YouTube, X, congress.gov), deposited once and inherited by every
// agent that connects over MCP.
//
// THE DASHBOARD NEVER SEES A VALUE. This panel shows presence, length,
// and annotation, the same tier the doctor reports secrets at. There is
// deliberately no reveal button: the human's copy of a value is the
// vault file itself (or wherever they got the key), and a round-trip
// through the browser would put secrets in DOM/devtools for no gain.
export function ResearchKeysPanel() {
  const [keys, setKeys] = useState<ResearchKey[] | null>(null);
  const [loadError, setLoadError] = useState("");
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const result = await api.listResearchKeys();
      setKeys(result.keys);
      setLoadError("");
    } catch (err) {
      // Keep whatever list we had; never render "no keys" off a failure.
      setLoadError(err instanceof ApiError ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const saved = await api.setResearchKey(name.trim(), value, note.trim());
      setNotice(
        `${saved.name} saved (${saved.length} chars). Agents can fetch it ` +
          "with get_research_key; the value is never shown here again.",
      );
      setName("");
      setValue("");
      setNote("");
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove(keyName: string) {
    if (!window.confirm(`Delete research key "${keyName}" for every agent on this account?`)) {
      return;
    }
    setError("");
    try {
      await api.deleteResearchKey(keyName);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
        Research API keys
      </h2>
      <p className="mt-2 text-xs text-zinc-500">
        One vault for the keys your research pipelines use (YouTube, X,
        congress.gov). Every connected agent can fetch them, each fetch is
        audited with a purpose, and values are never displayed here. Not
        for Lambda, S3, or LLM provider keys; those have their own homes
        above.
      </p>

      {loadError && (
        <p className="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800">
          {loadError}
        </p>
      )}

      {keys != null && keys.length === 0 && !loadError && (
        <p className="mt-3 rounded border border-dashed border-zinc-300 bg-zinc-50 px-3 py-3 text-center text-xs text-zinc-500">
          No research keys yet. Add one below, or have an agent deposit one
          with set_research_key.
        </p>
      )}

      {keys != null && keys.length > 0 && (
        <ul className="mt-3 space-y-2">
          {keys.map((k) => (
            <li
              key={k.name}
              className="rounded border border-zinc-200 bg-zinc-50 px-3 py-2"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-sm text-zinc-900">{k.name}</span>
                <div className="flex items-center gap-2">
                  <span className="font-mono text-[11px] tabular-nums text-zinc-500">
                    {k.present && k.length != null
                      ? `•••• ${k.length} chars`
                      : "value missing from vault file"}
                  </span>
                  <button
                    onClick={() => void remove(k.name)}
                    className="rounded border border-red-200 px-2 py-0.5 text-[11px] text-red-700 hover:bg-red-50"
                  >
                    Delete
                  </button>
                </div>
              </div>
              <p className="mt-1 text-[11px] text-zinc-500">
                {k.note ? `${k.note} · ` : ""}
                {k.created_by
                  ? `added by ${k.created_by}`
                  : "added outside Manifold (hand-edited file)"}
                {" · "}
                {k.last_used_at
                  ? `last fetched ${new Date(
                      k.last_used_at.endsWith("Z") || k.last_used_at.includes("+")
                        ? k.last_used_at
                        : `${k.last_used_at}Z`,
                    ).toLocaleString()}${k.last_used_by ? ` by ${k.last_used_by}` : ""}`
                  : "never fetched"}
              </p>
            </li>
          ))}
        </ul>
      )}

      <form onSubmit={submit} className="mt-3 space-y-2">
        <div className="flex gap-2">
          <input
            className="w-44 rounded border border-zinc-300 bg-white px-2.5 py-1.5 font-mono text-sm"
            placeholder="name (congress_gov)"
            value={name}
            onChange={(e) => setName(e.target.value)}
            pattern="[a-z][a-z0-9_]{0,62}"
            title="lowercase snake_case, 1-63 chars, starting with a letter"
            autoComplete="off"
            required
          />
          <input
            type="password"
            className="flex-1 rounded border border-zinc-300 bg-white px-2.5 py-1.5 font-mono text-sm"
            placeholder="paste the key value"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            autoComplete="off"
            required
          />
        </div>
        <div className="flex gap-2">
          <input
            className="flex-1 rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
            placeholder="note: what is this key for? (optional)"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            maxLength={200}
            autoComplete="off"
          />
          <button
            type="submit"
            disabled={busy || !name.trim() || !value}
            className="rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
          >
            {busy ? "Saving..." : "Save key"}
          </button>
        </div>
      </form>
      {notice && <p className="mt-2 text-xs text-emerald-700">{notice}</p>}
      {error && <p className="mt-2 text-xs text-red-700">{error}</p>}
    </section>
  );
}
