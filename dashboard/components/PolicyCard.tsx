"use client";

import { useEffect, useState } from "react";
import { api, type PolicyDoc, type PolicyRules } from "@/lib/api";

// The launch policy as ENFORCED right now (Phase 82). Deliberately
// read-only: the policy changes by editing policy.yaml and restarting,
// so every change is a reviewable commit, not a click. This card only
// answers "what will the orchestrator refuse, and under which file".
export function PolicyCard() {
  const [doc, setDoc] = useState<PolicyDoc | null>(null);

  useEffect(() => {
    api.policy().then(setDoc).catch(() => {});
  }, []);

  if (!doc) return null;

  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-5">
      <h3 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
        Launch policy
      </h3>
      {!doc.active ? (
        <p className="mt-2 text-xs text-zinc-500">
          No policy file. Everything the guardrails allow is allowed. To
          constrain instance types, regions, rates, or lifetimes per role,
          create a policy.yaml next to config.yaml; the template in the
          repo documents every rule.
        </p>
      ) : (
        <>
          <p className="mt-1 text-xs text-zinc-500">
            Enforced from <span className="font-mono">{doc.source}</span>.
            Changing it is an edit and a restart, so it stays a reviewed
            commit, never a setting.
          </p>
          <div className="mt-3 space-y-3">
            <RulesBlock label="every launch" rules={doc.launch} />
            {Object.entries(doc.roles).map(([role, rules]) => (
              <RulesBlock
                key={role}
                label={`role ${role} (tightens the above)`}
                rules={rules}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function RulesBlock({ label, rules }: { label: string; rules: PolicyRules }) {
  const lines: string[] = [];
  if (rules.allowed_instance_types.length)
    lines.push(`types: ${rules.allowed_instance_types.join(", ")}`);
  if (rules.allowed_regions.length)
    lines.push(`regions: ${rules.allowed_regions.join(", ")}`);
  if (rules.max_hourly_rate_usd)
    lines.push(`rate cap: $${rules.max_hourly_rate_usd.toFixed(2)}/hr per instance`);
  if (rules.require_max_lifetime) lines.push("max lifetime required");
  if (lines.length === 0) return null;
  return (
    <div className="rounded border border-zinc-200 bg-zinc-50 px-3 py-2">
      <p className="text-[11px] font-medium uppercase tracking-wide text-zinc-400">
        {label}
      </p>
      <ul className="mt-1 space-y-0.5">
        {lines.map((l) => (
          <li key={l} className="font-mono text-xs text-zinc-700">
            {l}
          </li>
        ))}
      </ul>
    </div>
  );
}
