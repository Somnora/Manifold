"use client";

import { useEffect, useState } from "react";
import { api, type Template, type TemplateParameter } from "@/lib/api";

// The whole point of this component: it renders ANY template's parameter
// schema. Adding a new template YAML requires zero frontend changes.
export function ParameterForm({
  template,
  onSubmit,
  submitting,
  initialValues,
  onModelChange,
}: {
  template: Template;
  onSubmit: (values: Record<string, unknown>) => void;
  submitting: boolean;
  // Seed field values (e.g. a model preset filling model_id). Remount the
  // form with a new `key` to re-seed; the user can still edit afterward.
  initialValues?: Record<string, string>;
  // Fires with the effective model_id value (typed, seeded, or default) so
  // the page can run the advisory model-vs-VRAM fit check. "" when the
  // template has no model_id parameter.
  onModelChange?: (model: string) => void;
}) {
  const [values, setValues] = useState<Record<string, string>>(
    () => ({ ...initialValues }),
  );
  
  const [renderedYaml, setRenderedYaml] = useState("");
  const [paramLineMapping, setParamLineMapping] = useState<Record<string, number[]>>({});
  const [focusedField, setFocusedField] = useState<string | null>(null);

  function currentValue(p: TemplateParameter): string {
    if (p.name in values) return values[p.name];
    return p.default != null ? String(p.default) : "";
  }

  const modelParam = template.parameters.find((p) => p.name === "model_id");
  const modelValue = modelParam ? currentValue(modelParam) : "";
  useEffect(() => {
    onModelChange?.(modelValue);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelValue]);

  // Debounced live render
  useEffect(() => {
    const payload: Record<string, unknown> = {};
    for (const p of template.parameters) {
      const raw = currentValue(p);
      if (raw === "") continue;
      payload[p.name] = raw;
    }
    
    const handler = setTimeout(() => {
      api.renderTemplate(template.name, payload).then((res) => {
        setRenderedYaml(res.rendered);
        setParamLineMapping(res.param_line_mapping || {});
      }).catch(err => {
        console.error("Render error:", err);
      });
    }, 300);
    
    return () => clearTimeout(handler);
  }, [values, template]);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const payload: Record<string, unknown> = {};
    for (const p of template.parameters) {
      const raw = currentValue(p);
      if (raw === "") continue; // let backend apply defaults / flag missing
      payload[p.name] = raw;    // backend coerces types from the schema
    }
    onSubmit(payload);
  }

  function handleEditAsConfig() {
    window.dispatchEvent(
      new CustomEvent("editCustomTemplate", {
        detail: {
          name: template.name + "-custom",
          yaml: renderedYaml,
        },
      })
    );
  }

  const field =
    "w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm transition-colors focus:border-teal-500 focus:ring-1 focus:ring-teal-500";

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <form onSubmit={handleSubmit} className="space-y-4">
        {template.parameters.map((p) => (
          <label key={p.name} className="block text-xs font-medium text-zinc-600">
            <span className="flex items-baseline justify-between mb-1.5">
              <span>
                {p.name}
                {p.required && <span className="ml-1 text-red-500">*</span>}
              </span>
              <span className="font-normal text-zinc-400">{p.type}</span>
            </span>
            {p.type === "boolean" ? (
              <select
                className={field}
                value={currentValue(p) || "false"}
                onFocus={() => setFocusedField(p.name)}
                onBlur={() => setFocusedField(null)}
                onChange={(e) =>
                  setValues((v) => ({ ...v, [p.name]: e.target.value }))
                }
              >
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            ) : (
              <input
                className={field}
                type={p.type === "string" ? "text" : "number"}
                step={p.type === "number" ? "any" : undefined}
                value={currentValue(p)}
                placeholder={p.default != null ? String(p.default) : ""}
                required={p.required}
                onFocus={() => setFocusedField(p.name)}
                onBlur={() => setFocusedField(null)}
                onChange={(e) =>
                  setValues((v) => ({ ...v, [p.name]: e.target.value }))
                }
              />
            )}
            <span className="mt-1 block font-normal text-zinc-400">
              {p.description}
            </span>
          </label>
        ))}
        <div className="pt-2">
          <button
            type="submit"
            disabled={submitting}
            className="rounded bg-zinc-900 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-zinc-800 disabled:opacity-50"
          >
            {submitting ? "Queueing..." : "Queue job"}
          </button>
        </div>
      </form>

      <div className="flex flex-col overflow-hidden rounded-lg border border-zinc-200 bg-[#09090b]">
        <div className="flex items-center justify-between border-b border-zinc-800 bg-zinc-900 px-3 py-2">
          <span className="text-xs font-medium tracking-wide text-zinc-400 uppercase">
            Rendered Config
          </span>
          <button
            type="button"
            onClick={handleEditAsConfig}
            className="text-xs text-teal-400 hover:text-teal-300 transition-colors"
            title="Switching to config editing keeps your values but leaves the form"
          >
            Edit as config
          </button>
        </div>
        <div className="relative flex-1 overflow-auto p-3 font-mono text-xs leading-relaxed">
          {renderedYaml ? (
            <pre className="text-[#e4e4e7]">
              {renderedYaml.split('\n').map((line, i) => {
                const lineNum = i + 1;
                const isHighlighted = focusedField && paramLineMapping[focusedField]?.includes(lineNum);
                return (
                  <div
                    key={i}
                    className={`-mx-3 px-3 ${
                      isHighlighted ? "bg-teal-900/40 text-teal-100" : ""
                    }`}
                  >
                    {line}
                  </div>
                );
              })}
            </pre>
          ) : (
            <div className="flex h-full items-center justify-center text-zinc-600">
              Loading preview...
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
