"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { Badge } from "@/components/Badge";
import { ConnectAgentPanel } from "@/components/ConnectAgentPanel";
import { PolicyCard } from "@/components/PolicyCard";
import { PolicySettings } from "@/components/PolicySettings";
import { PrincipalsPanel } from "@/components/PrincipalsPanel";

// First-run setup. Secrets are pasted here once, validated against Lambda,
// and written to .env on the machine running the backend. They are never
// displayed again, never logged, and never leave that machine.
export default function SettingsPage() {
  const { data: status, error, refresh } = usePolling(
    () => api.settingsStatus(),
    5000,
  );

  return (
    /* The full centered container, in two columns at lg. This page lived in
       a 672px column with no mx-auto - left-hugging inside the 1104px
       layout, with the dead space growing as the window did. items-start,
       not the default stretch: unequal cards must not inherit each other's
       height (the same grid lesson as the home page's fleet panel). */
    <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-2">
      {error && (
        <p className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800 lg:col-span-2">
          {error}
        </p>
      )}

      {status && (
        <section className="rounded-lg border border-zinc-200 bg-white p-4 lg:col-span-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
            Status
          </h2>
          <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 text-sm md:grid-cols-4">
            <StatusItem
              label="Mode"
              ok={!status.mock}
              okLabel="real"
              badLabel="mock (demo)"
              badTone="amber"
            />
            <StatusItem
              label="Lambda API key"
              ok={status.lambda_configured}
              okLabel="configured"
              badLabel="missing"
            />
            <StatusItem
              label="Google Cloud"
              ok={status.gcp_configured}
              okLabel="configured"
              badLabel="missing"
            />
            <StatusItem
              label="S3 storage keys"
              ok={status.s3_configured}
              okLabel="configured"
              badLabel="missing"
            />
            <StatusItem
              label="Tailscale"
              ok={status.tailscale_available}
              okLabel="available"
              badLabel="not set"
              badTone="zinc"
            />
            {/* Presence only, like every credential here: protected or
                open, never the token itself. */}
            <StatusItem
              label="API auth"
              ok={status.auth_required}
              okLabel="token required"
              badLabel="open (localhost)"
              badTone="zinc"
            />
          </dl>
          {status.mock && (
            <p className="mt-3 text-xs text-amber-700">
              Mock mode shows a demo catalog and never spends money. Keys
              saved below are validated and stored for real mode (start the
              backend without MANIFOLD_MOCK=1 to use them).
            </p>
          )}
          <p className="mt-2 text-xs text-zinc-400">
            Secrets are written to {status.env_path} and never shown again.
          </p>
        </section>
      )}

      {/* Two PACKED column stacks, not grid auto-placement: placement
          pairs cards by row, so a short card beside a tall one left a hole
          beneath it. Each stack flows tightly; reading order is the left
          column then the right, which suits settings. */}
      <div className="space-y-6">
        <LambdaKeyForm onSaved={refresh} />
        <S3KeysForm onSaved={refresh} />
        <PrincipalsPanel />
        <PolicySettings />
        <section className="rounded-lg border border-zinc-200 bg-white p-4 text-sm text-zinc-600">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Credits and billing
        </h2>
        <p className="mt-2 text-xs">
          Lambda does not expose credits or invoices through its API, so
          Manifold cannot show your remaining balance here. The Activity
          page tracks what Manifold itself spends; for the account-level
          view (credit balance, invoices, payment methods) use{" "}
          <a
            href="https://cloud.lambda.ai/settings/billing"
            target="_blank"
            rel="noreferrer"
            className="text-teal-600 hover:underline"
          >
            Lambda&apos;s billing page
          </a>
          .
        </p>
      </section>
      </div>

      <div className="space-y-6">
        <GcpConfigForm onSaved={refresh} />
        <ConnectAgentPanel envPath={status?.env_path} />
        <PolicyCard />
        <section className="rounded-lg border border-zinc-200 bg-white p-4 text-sm text-zinc-600">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
          Where do these come from?
        </h2>
        <ol className="mt-2 list-decimal space-y-1 pl-5 text-xs">
          <li>
            Create a Lambda account at cloud.lambda.ai, then generate an API
            key under <span className="font-mono">API keys</span>.
          </li>
          <li>
            Generate storage keys under{" "}
            <span className="font-mono">S3 Adapter Keys</span> (needed for
            the Storage page).
          </li>
          <li>
            Register an SSH key in the Lambda console. Persistent
            filesystems can be created right on the Storage page here.
          </li>
        </ol>
      </section>
      </div>
    </div>
  );
}

function StatusItem({
  label,
  ok,
  okLabel,
  badLabel,
  badTone = "red",
}: {
  label: string;
  ok: boolean;
  okLabel: string;
  badLabel: string;
  badTone?: "red" | "amber" | "zinc";
}) {
  return (
    <div>
      <dt className="text-xs text-zinc-400">{label}</dt>
      <dd className="mt-0.5">
        <Badge label={ok ? okLabel : badLabel} tone={ok ? "green" : badTone} />
      </dd>
    </div>
  );
}

function LambdaKeyForm({ onSaved }: { onSaved: () => void }) {
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api.setLambdaKey(key.trim());
      setNotice(
        `Key validated (${result.instance_types_visible} instance types visible)` +
          (result.applied_live
            ? " and applied; the launch form is live now."
            : " and saved for real mode."),
      );
      setKey("");
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
        Lambda API key
      </h2>
      <form onSubmit={submit} className="mt-3 flex gap-2">
        <input
          type="password"
          className="flex-1 rounded border border-zinc-300 bg-white px-2.5 py-1.5 font-mono text-sm"
          placeholder="paste your Lambda Cloud API key"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          autoComplete="off"
          required
          minLength={8}
        />
        <button
          type="submit"
          disabled={busy || key.trim().length < 8}
          className="rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
        >
          {busy ? "Validating..." : "Validate & save"}
        </button>
      </form>
      {notice && <p className="mt-2 text-xs text-emerald-700">{notice}</p>}
      {error && <p className="mt-2 text-xs text-red-700">{error}</p>}
    </section>
  );
}

function S3KeysForm({ onSaved }: { onSaved: () => void }) {
  const [accessKey, setAccessKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api.setS3Keys(accessKey.trim(), secretKey.trim());
      setNotice(
        result.validated
          ? "Keys validated against your filesystem and saved."
          : "Keys saved (no filesystem visible yet to validate against).",
      );
      setAccessKey("");
      setSecretKey("");
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
        S3 storage keys (for the Storage page)
      </h2>
      <form onSubmit={submit} className="mt-3 space-y-2">
        <input
          type="text"
          className="w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 font-mono text-sm"
          placeholder="access key id"
          value={accessKey}
          onChange={(e) => setAccessKey(e.target.value)}
          autoComplete="off"
        />
        <div className="flex gap-2">
          <input
            type="password"
            className="flex-1 rounded border border-zinc-300 bg-white px-2.5 py-1.5 font-mono text-sm"
            placeholder="secret access key"
            value={secretKey}
            onChange={(e) => setSecretKey(e.target.value)}
            autoComplete="off"
          />
          <button
            type="submit"
            disabled={busy || !accessKey.trim() || secretKey.trim().length < 8}
            className="rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
          >
            {busy ? "Saving..." : "Save"}
          </button>
        </div>
      </form>
      {notice && <p className="mt-2 text-xs text-emerald-700">{notice}</p>}
      {error && <p className="mt-2 text-xs text-red-700">{error}</p>}
    </section>
  );
}

function GcpConfigForm({ onSaved }: { onSaved: () => void }) {
  const [projectId, setProjectId] = useState("");
  const [zone, setZone] = useState("");
  const [credentialsPath, setCredentialsPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api.setGcpConfig(projectId.trim(), zone.trim(), credentialsPath.trim());
      setNotice("Google Cloud configuration saved.");
      setProjectId("");
      setZone("");
      setCredentialsPath("");
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-500">
        Google Cloud Configuration
      </h2>
      <form onSubmit={submit} className="mt-3 space-y-2">
        <input
          type="text"
          className="w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 font-mono text-sm"
          placeholder="Project ID (e.g. manifold-ai)"
          value={projectId}
          onChange={(e) => setProjectId(e.target.value)}
          autoComplete="off"
        />
        <div className="flex gap-2">
          <input
            type="text"
            className="flex-1 rounded border border-zinc-300 bg-white px-2.5 py-1.5 font-mono text-sm"
            placeholder="Default Zone (e.g. us-central1-a)"
            value={zone}
            onChange={(e) => setZone(e.target.value)}
            autoComplete="off"
          />
          <input
            type="text"
            className="flex-1 rounded border border-zinc-300 bg-white px-2.5 py-1.5 font-mono text-sm"
            placeholder="Credentials JSON Path (absolute path)"
            value={credentialsPath}
            onChange={(e) => setCredentialsPath(e.target.value)}
            autoComplete="off"
          />
          <button
            type="submit"
            disabled={busy || !projectId.trim() || !zone.trim() || !credentialsPath.trim()}
            className="rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
          >
            {busy ? "Saving..." : "Save"}
          </button>
        </div>
      </form>
      {notice && <p className="mt-2 text-xs text-emerald-700">{notice}</p>}
      {error && <p className="mt-2 text-xs text-red-700">{error}</p>}
    </section>
  );
}
