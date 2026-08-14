"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type Brain, type DistillConfig } from "@/lib/api";

// Phase 84: draft the training config for a distillation run in plain words.
// A training config is the YAML settings file a fine-tune reads: which base
// model to start from, which dataset file to train on, and how long to run.
// A brain writes the draft, the backend checks it is valid YAML carrying the
// keys a fine-tune needs, and it lands here as text to READ. Manifold does
// not save it and does not train from it: both of those stay your move.
// `connectedInstances` is only used to warn honestly: the persistent
// filesystem is reachable through a running instance's file browser, so
// with none connected there is nowhere to put the config yet.
export function DistillConfigPanel({
  connectedInstances,
}: {
  connectedInstances: number;
}) {
  const [open, setOpen] = useState(false);
  const [brains, setBrains] = useState<Brain[]>([]);
  const [brain, setBrain] = useState("");
  const [spec, setSpec] = useState("");
  const [dataset, setDataset] = useState("");
  const [student, setStudent] = useState("");
  const [result, setResult] = useState<DistillConfig | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // Loaded when the panel is opened, not on every Jobs-page render: this is
  // a rarely-used drawer and the brain list costs a round trip.
  useEffect(() => {
    if (!open) return;
    api
      .brains()
      .then((list) => {
        setBrains(list);
        setBrain((v) => v || list[0]?.ref || "");
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, [open]);

  const chosen = brains.find((b) => b.ref === brain);

  // navigator.clipboard is undefined on a page served over plain HTTP from
  // anything but localhost, so this throws rather than rejecting: catch it
  // here and say so, the way the file browser does.
  async function copyYaml(yaml: string) {
    try {
      await navigator.clipboard.writeText(yaml);
      setCopied(true);
    } catch {
      setError("Could not access the clipboard. Select the YAML and copy it.");
    }
  }

  async function draft() {
    setBusy(true);
    setError("");
    setResult(null);
    setCopied(false);
    try {
      const cfg = await api.distillConfig({
        spec: spec.trim(),
        brain,
        ...(dataset.trim() ? { dataset: dataset.trim() } : {}),
        ...(student.trim() ? { student: student.trim() } : {}),
      });
      setResult(cfg);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
          Training config
        </h3>
        <button
          onClick={() => setOpen((s) => !s)}
          className="shrink-0 rounded border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50"
        >
          {open ? "Close" : "Draft a config"}
        </button>
      </div>

      <p className="mt-2 text-xs text-zinc-500">
        A training config is the YAML settings file a fine-tune reads: which
        base model to start from, which dataset file to train on, and how long
        to run. Describe the job in plain words and a brain drafts one for you
        to review.
      </p>

      {open && (
        <div className="mt-3 space-y-3">
          {brains.length === 0 ? (
            <p className="rounded border border-zinc-200 bg-zinc-50 px-3 py-2 text-xs text-zinc-500">
              No brain available to write it. A brain can be a model served on
              a running instance (queue{" "}
              <span className="font-mono">vllm-serve</span> above), a local
              model server on this machine (Ollama or LM Studio, detected
              automatically), or a frontier API with a key added in Settings.
            </p>
          ) : (
            <>
              <label className="block text-xs font-medium text-zinc-600">
                Brain
                <select
                  className="mt-1 block w-full min-w-0 max-w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
                  value={brain}
                  onChange={(e) => setBrain(e.target.value)}
                >
                  {brains.map((b) => (
                    <option key={b.ref} value={b.ref}>
                      {b.label}
                    </option>
                  ))}
                </select>
              </label>

              <label className="block text-xs font-medium text-zinc-600">
                What should this model learn to do
                <textarea
                  className="mt-1 w-full rounded border border-zinc-300 px-2.5 py-1.5 text-sm"
                  rows={3}
                  value={spec}
                  onChange={(e) => setSpec(e.target.value)}
                  placeholder='e.g. "distill film-shot tagging into a 3B LoRA that fits an A10, dataset kept-tags.jsonl"'
                />
              </label>

              <div className="flex flex-wrap gap-3">
                <label className="block min-w-0 flex-1 text-xs font-medium text-zinc-600">
                  Dataset file (optional)
                  <input
                    className="mt-1 block w-full min-w-0 rounded border border-zinc-300 px-2.5 py-1.5 text-sm"
                    value={dataset}
                    onChange={(e) => setDataset(e.target.value)}
                    placeholder="kept-tags.jsonl"
                    title="A file under <filesystem>/synthesized, which is where llm-synthesize and llm-judge write their training sets."
                  />
                </label>
                <label className="block min-w-0 flex-1 text-xs font-medium text-zinc-600">
                  Student model (optional)
                  <input
                    className="mt-1 block w-full min-w-0 rounded border border-zinc-300 px-2.5 py-1.5 text-sm"
                    value={student}
                    onChange={(e) => setStudent(e.target.value)}
                    placeholder="leave empty and the brain picks one"
                    title="The small open base model the LoRA trains on top of. Left empty, the brain recommends one that fits the GPU you described."
                  />
                </label>
              </div>

              <div className="flex items-center justify-between gap-3">
                <p className="text-[11px] text-zinc-400">
                  Nothing here starts a training run. The draft comes back as
                  text to read.
                </p>
                <button
                  onClick={draft}
                  disabled={busy || spec.trim().length < 10 || !brain}
                  className="shrink-0 rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
                >
                  {busy
                    ? `Asking ${chosen?.label ?? "the brain"}...`
                    : "Draft config"}
                </button>
              </div>
              {busy && (
                <p className="text-[11px] text-zinc-400">
                  A brain that runs as a CLI on this machine can take a few
                  minutes to answer. Leave this open.
                </p>
              )}
            </>
          )}

          {result && (
            <div className="space-y-2 border-t border-zinc-100 pt-3">
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="font-medium text-zinc-600">Student</span>
                <span className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-[11px] text-zinc-700">
                  {result.student}
                </span>
                <button
                  onClick={() => copyYaml(result.config_yaml)}
                  className="ml-auto shrink-0 rounded border border-zinc-300 px-2 py-0.5 text-xs text-zinc-700 hover:bg-zinc-50"
                >
                  {copied ? "Copied" : "Copy YAML"}
                </button>
              </div>
              {result.notes && (
                <p className="whitespace-pre-wrap text-xs text-zinc-600">
                  {result.notes}
                </p>
              )}
              {/* Read-only on purpose: this is a preview of what the brain
                  wrote, not an editor. Literal hex colors, as in the template
                  editor: the dark theme remaps the zinc scale, so
                  text-zinc-100 on bg-zinc-950 renders ink on ink. */}
              <pre className="max-h-80 overflow-auto whitespace-pre rounded border border-zinc-300 bg-[#09090b] p-3 font-mono text-xs leading-relaxed text-[#e4e4e7]">
                {result.config_yaml}
              </pre>
              <div className="rounded border border-zinc-200 bg-zinc-50 px-3 py-2 text-[11px] leading-relaxed text-zinc-600">
                <p className="font-medium text-zinc-700">
                  Manifold has not saved this and will not train from it.
                </p>
                <p className="mt-1">
                  To use it: copy the YAML, save it on this machine as a
                  .yaml file, then open Browse on a connected instance (from
                  its card on Instances), navigate to{" "}
                  <span className="font-mono">configs/</span> and choose
                  Upload here. Once the file sits at{" "}
                  <span className="font-mono">
                    &lt;filesystem&gt;/configs/your-config.yaml
                  </span>
                  , queue <span className="font-mono">axolotl-finetune</span>{" "}
                  above with <span className="font-mono">config_path</span>{" "}
                  set to that file name. Read it before you do: it is a first
                  draft from a model, and running it spends GPU time.
                </p>
                {connectedInstances === 0 && (
                  <p className="mt-1 text-amber-700">
                    No instance is connected, so there is nowhere to upload it
                    yet. Copy the YAML and keep it; the filesystem becomes
                    reachable once an instance is running.
                  </p>
                )}
              </div>
            </div>
          )}

          {error && (
            <p className="whitespace-pre-wrap rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800">
              {error}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
