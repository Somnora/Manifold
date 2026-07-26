"use client";

import { useState, useEffect } from "react";
import { api, ApiError, type Template } from "@/lib/api";

// Author your own job templates, right where you queue jobs. A template is
// a YAML recipe - image, command with {{param}} placeholders, a parameter
// schema, mounts - and a custom one is validated by the SAME loader and
// jail as the bundled set (mounts only under /workspace/ephemeral or
// {persistent}; ports always loopback). Write one by hand, or ask any agent
// connected over MCP to build it from a workflow you proved together - once
// saved it is a one-click job with a parameter form, no agent needed again.
const STARTER = `name: my-template
description: What this job does, in one line.
image: pytorch/pytorch:2.4.0-cuda12.4-cudnn9-runtime
# {{placeholders}} are filled from the parameter form and shell-quoted.
command: >-
  bash -c 'echo processing {{input_dir}}' argv0
parameters:
  - name: input_dir
    type: string
    description: Directory under the filesystem to process
    # no default = required
volumes:
  # {persistent} = your Lambda filesystem; /workspace/ephemeral = scratch.
  - host: "{persistent}/outputs"
    container: /out
`;

export function TemplateEditor({
  templates,
  onChanged,
}: {
  templates: Template[];
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [yaml, setYaml] = useState(STARTER);
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const custom = templates.filter((t) => t.custom);

  function startNew() {
    setEditing(null);
    setYaml(STARTER);
    setOpen(true);
    setError("");
    setNotice("");
  }

  function startEdit(t: Template | { name: string; yaml: string }) {
    setEditing(t.name);
    setYaml(t.yaml || "");
    setOpen(true);
    setError("");
    setNotice("");
  }

  // Listen for 'editCustomTemplate' event to edit an ad-hoc or promoted template.
  // This allows ParameterForm (or other components) to trigger the editor with pre-rendered YAML.
  useEffect(() => {
    const handler = (e: Event) => {
      const customEvent = e as CustomEvent;
      if (customEvent.detail && customEvent.detail.yaml) {
        startEdit({ name: customEvent.detail.name || "promoted-template", yaml: customEvent.detail.yaml });
      }
    };
    window.addEventListener("editCustomTemplate", handler);
    return () => window.removeEventListener("editCustomTemplate", handler);
  }, []);

  async function save() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const saved = await api.saveCustomTemplate(yaml);
      setNotice(`Saved '${saved.name}' - it is live in the template list now.`);
      setEditing(saved.name);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove(name: string) {
    setError("");
    try {
      await api.deleteCustomTemplate(name);
      if (editing === name) {
        setEditing(null);
        setYaml(STARTER);
      }
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  return (
    <section className="rounded-lg border border-zinc-200 bg-white p-4">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
          Custom templates{custom.length > 0 ? ` (${custom.length})` : ""}
        </h3>
        <div className="flex gap-2">
          <button
            onClick={startNew}
            className="rounded border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50"
          >
            New template
          </button>
          {open && (
            <button
              onClick={() => setOpen(false)}
              className="rounded border border-zinc-300 px-2.5 py-1 text-xs text-zinc-600 hover:bg-zinc-50"
            >
              Close
            </button>
          )}
        </div>
      </div>

      {custom.length > 0 && (
        <ul className="mt-2 space-y-1">
          {custom.map((t) => (
            <li
              key={t.name}
              className="flex items-center justify-between gap-3 rounded border border-zinc-100 bg-zinc-50 px-2.5 py-1.5 text-sm"
            >
              <span className="min-w-0 truncate">
                <span className="font-mono font-medium">{t.name}</span>
                <span className="ml-2 text-xs text-zinc-500">
                  {t.description}
                </span>
              </span>
              <span className="flex shrink-0 gap-2 text-xs">
                <button
                  onClick={() => startEdit(t)}
                  className="text-zinc-600 hover:text-zinc-900"
                >
                  Edit
                </button>
                <button
                  onClick={() => remove(t.name)}
                  className="text-red-700 hover:text-red-900"
                >
                  Delete
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}

      {!open && custom.length === 0 && (
        <p className="mt-2 text-xs text-zinc-400">
          Turn any workflow into a one-click job: your own image, command, and
          parameter form. Write the YAML here, or ask an agent (MCP{" "}
          <span className="font-mono">save_template</span>) to draft it from a
          workflow you proved together - then rerun it forever without the
          agent.
        </p>
      )}

      {open && (
        <div className="mt-3 space-y-2">
          {/* Literal hex colors, deliberately: the dark theme REMAPS the
              zinc scale (zinc-100 is near-black here), so text-zinc-100 on
              bg-zinc-950 rendered ink-on-ink. This box is a terminal-style
              editor; give it the terminal's own fixed palette. */}
          <textarea
            value={yaml}
            onChange={(e) => setYaml(e.target.value)}
            rows={16}
            spellCheck={false}
            className="w-full rounded border border-zinc-300 bg-[#09090b] p-3 font-mono text-xs leading-relaxed text-[#e4e4e7] caret-teal-400 placeholder:text-[#71717a]"
          />
          <div className="flex items-center justify-between gap-3">
            <p className="text-[11px] text-zinc-400">
              Validated on save: mounts only under{" "}
              <span className="font-mono">{"{persistent}"}</span> or{" "}
              <span className="font-mono">/workspace/ephemeral</span>; ports
              always bind to loopback. Same rules as the built-in templates.
            </p>
            <button
              onClick={save}
              disabled={busy || yaml.trim().length < 20}
              className="shrink-0 rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
            >
              {busy ? "Validating..." : editing ? "Save changes" : "Save template"}
            </button>
          </div>
          {notice && <p className="text-xs text-emerald-700">{notice}</p>}
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
