"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type Preferences } from "@/lib/api";

// The account's default cloud (Phase 102): where a launch goes when nobody
// names a provider. Agents over MCP, the bridge, and the launch form on
// first paint all follow it, so this one control moves a whole project
// from one cloud to another without anyone re-learning anything.
//
// It is deliberately the owner's switch and has no MCP tool: an agent
// quietly redirecting where the money goes is not a setting, it is a
// footgun. Agents can still override per launch.
const LABELS: Record<string, string> = {
  lambda: "Lambda AI",
  gcp: "Google Cloud",
};

export function DefaultProviderPanel({
  lambdaConfigured,
  gcpConfigured,
}: {
  // undefined = the status has not loaded yet. Left undefined rather than
  // defaulted to false: "we have not asked" and "it is not set up" are
  // different answers and only one of them should be printed.
  lambdaConfigured?: boolean;
  gcpConfigured?: boolean;
}) {
  const [prefs, setPrefs] = useState<Preferences | null>(null);
  const [providers, setProviders] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api
      .preferences()
      .then((r) => {
        setPrefs(r.preferences);
        setProviders(r.registered_providers);
      })
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : String(err)),
      );
  }, []);

  async function choose(name: string) {
    if (!prefs || name === prefs.providers.default_provider) return;
    setError("");
    try {
      const updated = await api.updatePreferences({
        providers: { default_provider: name },
      });
      setPrefs(updated);
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
    } catch (err) {
      // The backend refuses a provider it has not registered. Show its
      // words and leave the stored choice alone.
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  function configured(name: string): boolean | undefined {
    if (name === "lambda") return lambdaConfigured;
    if (name === "gcp") return gcpConfigured;
    return undefined;
  }

  if (!prefs) {
    return (
      <section className="rounded-lg border border-zinc-200 bg-white p-4 text-sm text-zinc-500">
        {error || "Loading default provider..."}
      </section>
    );
  }

  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4">
      <div className="flex items-baseline justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Default provider
        </h2>
        {saved && (
          <span className="font-mono text-[11px] text-teal-400">saved</span>
        )}
      </div>
      <p className="mt-1 text-xs text-zinc-500">
        Which cloud a launch lands on when nobody names one. Every agent
        connected over MCP follows this, so moving a project to another
        cloud is this one choice rather than a change of habit for each
        agent. A launch that names a provider still wins, and jobs already
        queued, capacity watches, Autopilot runs and clusters keep the
        cloud they were created for.
      </p>

      <div className="mt-3 space-y-2">
        {providers.map((name) => {
          const ready = configured(name);
          return (
            <label
              key={name}
              className="flex cursor-pointer items-start gap-2.5"
            >
              <input
                type="radio"
                name="default-provider"
                checked={prefs.providers.default_provider === name}
                onChange={() => void choose(name)}
                className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-teal-400"
              />
              <span className="min-w-0">
                <span className="text-sm text-zinc-800">
                  {LABELS[name] ?? name}
                </span>
                {ready === false && (
                  <span className="ml-1.5 text-xs text-amber-700">
                    not configured yet:{" "}
                    {name === "gcp"
                      ? "needs gcloud ADC and a project id in Settings"
                      : "needs its credentials in Settings"}
                  </span>
                )}
                {ready === true && (
                  <span className="ml-1.5 text-xs text-zinc-400">
                    configured
                  </span>
                )}
              </span>
            </label>
          );
        })}
      </div>

      {configured(prefs.providers.default_provider) === false && (
        <p className="mt-3 rounded border border-amber-300/40 bg-amber-50 px-2 py-1 text-[11px] text-amber-700">
          Launches that do not name a cloud will be refused with the setup
          step until this one is configured. Nothing is lost: the refusal
          says what to do, and naming a provider on the launch still works.
        </p>
      )}

      {error && <p className="mt-2 text-xs text-red-700">{error}</p>}
    </section>
  );
}
