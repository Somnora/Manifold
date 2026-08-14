"use client";

import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiError,
  type Instance,
  type LocalModelLibrary,
} from "@/lib/api";
import { formatBytes } from "@/lib/format";

// Phase 85: the last mile of a distillation. The model you trained lives on
// a filesystem you pay to reach; this brings it home, installs it into
// Ollama, and from there it appears in the brain picker on its own - the
// backend already probes 127.0.0.1:11434, so nothing new had to be taught
// about it. Everything here except the pull is free and local.
export function OwnItLocallyPanel({ connected }: { connected: Instance[] }) {
  const [open, setOpen] = useState(false);
  const [library, setLibrary] = useState<LocalModelLibrary | null>(null);
  const [pullName, setPullName] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const refresh = useCallback(() => {
    api
      .localModels()
      .then(setLibrary)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, []);

  useEffect(() => {
    if (open) refresh();
  }, [open, refresh]);

  async function pull() {
    if (connected.length === 0) return;
    setBusy("pull");
    setError("");
    setNote("");
    try {
      const r = await api.pullModel(connected[0].id, pullName.trim());
      setNote(`Pulled ${r.name} (${formatBytes(r.bytes)}) to your library.`);
      setPullName("");
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }

  async function install(name: string) {
    setBusy(name);
    setError("");
    setNote("");
    try {
      const r = await api.installModel({ name });
      // The payoff line: it is a brain now, and the picker will find it
      // within the local-endpoint detection window.
      setNote(
        `${r.ollama_name} is installed. It appears in the brain picker as ` +
          `"${r.brain_ref}" within about ten seconds.`,
      );
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-zinc-800">
            Own it locally
          </h3>
          <p className="mt-0.5 text-xs text-zinc-500">
            Bring a quantized model home, run it for $0, and use it as a brain.
          </p>
        </div>
        <button
          onClick={() => setOpen((v) => !v)}
          className="shrink-0 rounded border border-zinc-300 px-2.5 py-1 text-xs text-zinc-700 hover:bg-zinc-50"
        >
          {open ? "Hide" : "Open"}
        </button>
      </div>

      {open && (
        <div className="mt-3 space-y-3">
          <div className="flex flex-wrap items-end gap-2">
            <label className="block min-w-0 flex-1 text-xs font-medium text-zinc-600">
              Pull a .gguf from the instance
              <input
                className="mt-1 block w-full min-w-0 rounded border border-zinc-300 px-2.5 py-1.5 text-sm"
                value={pullName}
                onChange={(e) => setPullName(e.target.value)}
                placeholder="my-student.gguf"
                title="The gguf-quantize output, a filename under <filesystem>/models."
              />
            </label>
            <button
              onClick={pull}
              disabled={
                busy !== "" || !pullName.trim() || connected.length === 0
              }
              className="shrink-0 rounded bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
            >
              {busy === "pull" ? "Pulling..." : "Pull"}
            </button>
          </div>

          {connected.length === 0 && (
            <p className="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-800">
              Downloads ride an instance&apos;s managed connection, so{" "}
              <strong>pull before you terminate</strong>. With no instance
              running the filesystem is unreachable.
            </p>
          )}

          {library && (
            <>
              <div className="rounded border border-zinc-200">
                {library.models.length === 0 ? (
                  <p className="px-3 py-2 text-xs text-zinc-500">
                    Nothing in your library yet. Run{" "}
                    <span className="font-mono">gguf-quantize</span> on the
                    merged model, then pull the .gguf it writes.
                  </p>
                ) : (
                  <ul className="divide-y divide-zinc-100">
                    {library.models.map((m) => (
                      <li
                        key={m.name}
                        className="flex flex-wrap items-center gap-2 px-3 py-2 text-xs"
                      >
                        <span className="font-mono text-zinc-800">
                          {m.name}
                        </span>
                        <span className="text-zinc-500">
                          {formatBytes(m.size_bytes)}
                        </span>
                        {m.installed ? (
                          <span className="ml-auto rounded bg-emerald-50 px-1.5 py-0.5 text-[11px] text-emerald-700">
                            in Ollama as {m.suggested_ollama_name}
                          </span>
                        ) : (
                          <button
                            onClick={() => install(m.name)}
                            disabled={busy !== "" || !library.ollama_available}
                            className="ml-auto shrink-0 rounded border border-zinc-300 px-2 py-0.5 text-[11px] text-zinc-700 hover:bg-zinc-50 disabled:opacity-50"
                            title={
                              library.ollama_available
                                ? `Installs as ${m.suggested_ollama_name}`
                                : "Ollama is not installed on this machine."
                            }
                          >
                            {busy === m.name
                              ? "Installing..."
                              : "Install into Ollama"}
                          </button>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <p className="text-[11px] leading-relaxed text-zinc-500">
                Library:{" "}
                <span className="font-mono">{library.library_path}</span>.{" "}
                {library.ollama_available ? (
                  <>
                    Installing registers the file with Ollama and it becomes a
                    brain you can pick anywhere in Manifold, running on your
                    own machine for $0.
                  </>
                ) : (
                  <>
                    Ollama is not installed, so the file is yours but nothing
                    here can run it yet. Install Ollama from ollama.com, or
                    open the .gguf directly in LM Studio.
                  </>
                )}
              </p>
            </>
          )}

          {note && (
            <p className="rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
              {note}
            </p>
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
