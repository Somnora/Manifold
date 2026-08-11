"use client";

import { useState } from "react";
import { ApiError, api } from "@/lib/api";

// The first run.
//
// Two constraints shaped this. First, demo mode CANNOT be offered as a
// button: mock mode is decided when the backend process starts (it swaps
// every client and uses a different database file), and restarting into it
// deliberately refuses to boot when a live launch exists. A button that
// could leave someone with no backend at all is worse than a command they
// paste knowingly. Second, the walkthrough sets a spending cap before it
// ever offers a GPU, because the guardrails are the reason this tool
// exists and burying them in Settings means most people never meet them.

type Step = "welcome" | "key" | "guardrails" | "done";

export function Onboarding({
  envPath,
  onFinished,
}: {
  envPath: string;
  onFinished: () => void;
}) {
  const [step, setStep] = useState<Step>("welcome");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function finish(completed: boolean) {
    setBusy(true);
    try {
      await api.updatePreferences({
        onboarding: {
          completed,
          dismissed_at: completed ? "" : new Date().toISOString(),
        },
      });
    } catch {
      // A failed write here must not trap someone in the wizard.
    } finally {
      setBusy(false);
      onFinished();
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-zinc-950/60 p-4 backdrop-blur-sm sm:items-center">
      <div className="w-full max-w-xl rounded-lg border border-zinc-200 bg-white p-6 shadow-xl">
        <div className="mb-4 flex items-center justify-between">
          <p className="font-mono text-xs uppercase tracking-widest text-zinc-400">
            Manifold setup
          </p>
          <button
            onClick={() => finish(false)}
            disabled={busy}
            className="text-xs text-zinc-500 underline hover:text-zinc-900 disabled:opacity-50"
          >
            Skip for now
          </button>
        </div>

        {step === "welcome" && (
          <Welcome onNext={() => setStep("key")} envPath={envPath} />
        )}
        {step === "key" && (
          <KeyStep
            busy={busy}
            setBusy={setBusy}
            error={error}
            setError={setError}
            onNext={() => setStep("guardrails")}
          />
        )}
        {step === "guardrails" && (
          <GuardrailStep
            busy={busy}
            setBusy={setBusy}
            error={error}
            setError={setError}
            onNext={() => setStep("done")}
          />
        )}
        {step === "done" && <Done onClose={() => finish(true)} busy={busy} />}

        <ol className="mt-6 flex items-center gap-2 text-[11px] text-zinc-400">
          {(["welcome", "key", "guardrails", "done"] as Step[]).map((s, i) => (
            <li
              key={s}
              className={`flex items-center gap-2 ${
                s === step ? "text-zinc-900" : ""
              }`}
            >
              <span
                className={`inline-block h-1.5 w-1.5 rounded-full ${
                  s === step ? "bg-zinc-900" : "bg-zinc-300"
                }`}
              />
              {i < 3 && <span className="text-zinc-200">·</span>}
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}

function Welcome({
  onNext,
  envPath,
}: {
  onNext: () => void;
  envPath: string;
}) {
  return (
    <>
      <h2 className="text-lg font-medium text-zinc-900">
        Rent GPUs without losing track of the meter
      </h2>
      <p className="mt-2 text-sm text-zinc-600">
        Manifold runs on your machine and drives cloud GPUs through one guarded
        gateway. Three things it does whether or not you are watching:
      </p>
      <ul className="mt-3 space-y-2 text-sm text-zinc-600">
        <li>
          <span className="font-medium text-zinc-900">Refuses to overspend.</span>{" "}
          Every launch passes a concurrency and an hourly-rate check first.
        </li>
        <li>
          <span className="font-medium text-zinc-900">Saves before it destroys.</span>{" "}
          Terminating an instance rescues its unsaved files, and stops if
          something could not be saved.
        </li>
        <li>
          <span className="font-medium text-zinc-900">Tells you what it cost.</span>{" "}
          Or admits it does not know, which is the more useful answer when a
          launch failed halfway.
        </li>
      </ul>

      <div className="mt-4 rounded border border-zinc-200 bg-zinc-50 p-3">
        <p className="text-xs font-medium text-zinc-700">
          Want to look around first, with no account and no spend?
        </p>
        <p className="mt-1 text-xs text-zinc-500">
          Restart the backend in demo mode and it serves a fixture catalog and
          a month of example history:
        </p>
        <code className="mt-2 block overflow-x-auto rounded bg-zinc-950 px-3 py-2 font-mono text-[11px] text-zinc-100">
          MANIFOLD_MOCK=1 MANIFOLD_MOCK_SEED_DAYS=30 uv run uvicorn
          app.main:create_default_app --factory
        </code>
        <p className="mt-1 text-[11px] text-zinc-400">
          It is a command rather than a button on purpose: switching modes
          restarts the backend, and Manifold refuses to do that while a real
          instance may still be running.
        </p>
      </div>

      <p className="mt-3 text-[11px] text-zinc-400">
        Credentials are written to {envPath} and never shown again.
      </p>

      <button
        onClick={onNext}
        className="mt-4 rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700"
      >
        Connect a Lambda account
      </button>
    </>
  );
}

function KeyStep({
  busy,
  setBusy,
  error,
  setError,
  onNext,
}: {
  busy: boolean;
  setBusy: (b: boolean) => void;
  error: string;
  setError: (e: string) => void;
  onNext: () => void;
}) {
  const [key, setKey] = useState("");
  const [notice, setNotice] = useState("");

  async function save() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const res = await api.setLambdaKey(key.trim());
      setNotice(
        `Key validated: ${res.instance_types_visible} instance types visible.`,
      );
      setTimeout(onNext, 700);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h2 className="text-lg font-medium text-zinc-900">
        Your Lambda API key
      </h2>
      <p className="mt-2 text-sm text-zinc-600">
        Manifold checks the key against Lambda before saving it, so you find
        out now rather than at the first launch. From the Lambda Cloud console,
        under API keys.
      </p>
      <input
        type="password"
        autoComplete="off"
        value={key}
        onChange={(e) => setKey(e.target.value)}
        placeholder="secret_..."
        className="mt-3 w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 font-mono text-sm"
      />
      {notice && <p className="mt-2 text-xs text-emerald-700">{notice}</p>}
      {error && <p className="mt-2 text-xs text-red-700">{error}</p>}
      <div className="mt-4 flex items-center gap-3">
        <button
          onClick={save}
          disabled={busy || key.trim().length < 8}
          className="rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
        >
          {busy ? "Checking..." : "Validate and save"}
        </button>
        <button
          onClick={onNext}
          className="text-xs text-zinc-500 underline hover:text-zinc-900"
        >
          I will add it later
        </button>
      </div>
    </>
  );
}

function GuardrailStep({
  busy,
  setBusy,
  error,
  setError,
  onNext,
}: {
  busy: boolean;
  setBusy: (b: boolean) => void;
  error: string;
  setError: (e: string) => void;
  onNext: () => void;
}) {
  const [hourly, setHourly] = useState("4.00");
  const [monthly, setMonthly] = useState("200");

  async function save() {
    setBusy(true);
    setError("");
    try {
      await api.updatePreferences({
        guardrails: {
          max_hourly_spend_usd: Number(hourly) || 0,
          monthly_budget_usd: Number(monthly) || 0,
        },
      });
      onNext();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h2 className="text-lg font-medium text-zinc-900">Set your limits</h2>
      <p className="mt-2 text-sm text-zinc-600">
        These are the numbers the guards enforce. You can change them any time
        under Settings.
      </p>

      <label className="mt-4 block text-sm">
        <span className="text-zinc-700">Maximum hourly burn</span>
        <input
          value={hourly}
          onChange={(e) => setHourly(e.target.value)}
          inputMode="decimal"
          className="mt-1 w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm tabular-nums"
        />
        <span className="mt-1 block text-xs text-zinc-500">
          A launch that would push your running instances above this is
          refused, before any API call. This one is enforced.
        </span>
      </label>

      <label className="mt-4 block text-sm">
        <span className="text-zinc-700">Monthly budget</span>
        <input
          value={monthly}
          onChange={(e) => setMonthly(e.target.value)}
          inputMode="decimal"
          className="mt-1 w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm tabular-nums"
        />
        <span className="mt-1 block text-xs text-zinc-500">
          Reported, not enforced: you get a burn-down and a warning as you
          approach it, but it never blocks a launch. It only counts instances
          Manifold started, so it is a floor. Leave it at 0 for no budget.
        </span>
      </label>

      {error && <p className="mt-2 text-xs text-red-700">{error}</p>}

      <button
        onClick={save}
        disabled={busy}
        className="mt-4 rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
      >
        {busy ? "Saving..." : "Save limits"}
      </button>
    </>
  );
}

function Done({ onClose, busy }: { onClose: () => void; busy: boolean }) {
  return (
    <>
      <h2 className="text-lg font-medium text-zinc-900">You are set up</h2>
      <p className="mt-2 text-sm text-zinc-600">
        Launch a GPU from the form on the Instances page. A few things worth
        knowing on the first run:
      </p>
      <ul className="mt-3 space-y-2 text-sm text-zinc-600">
        <li>
          Large instances take 15 to 40 minutes to boot, and they bill for that
          time. Manifold shows the boot component separately.
        </li>
        <li>
          Idle instances terminate themselves after 30 minutes by default, and
          rescue their files first.
        </li>
        <li>
          You can set a maximum lifetime on a launch. Nothing running on the
          box can extend it.
        </li>
      </ul>
      <button
        onClick={onClose}
        disabled={busy}
        className="mt-4 rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
      >
        Start using Manifold
      </button>
    </>
  );
}
