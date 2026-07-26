"use client";

import { useEffect, useMemo, useState } from "react";
import {
  api,
  ApiError,
  type Filesystem,
  type InstanceTypeInfo,
  type Region,
} from "@/lib/api";
import { formatMoney } from "@/lib/format";

// Guided order of operations, mirroring Lambda's own console:
//   1. Pick a GPU — available types first, cheapest to priciest; the ones
//      that are out of capacity are greyed out and unselectable.
//   2. Pick a region — only the regions where that GPU is available are
//      selectable; the rest are greyed with "not available for this type".
//   3. Filesystem narrows to the chosen region (they are region-locked) —
//      or "scratch only": no filesystem at all, launchable in ANY region
//      with capacity. Everything on a scratch-only instance dies with it,
//      so the form says so in amber before the click, and the data-safety
//      rescue is the only net (download-to-this-machine, Settings).
// The form only collects input; every rule (region match, budget,
// concurrency) is still enforced by the backend and its rejection shown
// verbatim.

// Sentinel for "launch without a filesystem" ("" = nothing chosen yet).
const SCRATCH_ONLY = "__scratch_only__";

export function LaunchForm({ onLaunched }: { onLaunched: () => void }) {
  const [types, setTypes] = useState<Record<string, InstanceTypeInfo>>({});
  const [regions, setRegions] = useState<Region[]>([]);
  const [filesystems, setFilesystems] = useState<Filesystem[]>([]);
  const [sshKeys, setSshKeys] = useState<string[]>([]);
  const [instanceType, setInstanceType] = useState("");
  const [region, setRegion] = useState("");
  const [filesystem, setFilesystem] = useState("");
  const [sshKey, setSshKey] = useState("");
  const [mode, setMode] = useState("direct-ssh");
  const [idleTimeout, setIdleTimeout] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    Promise.all([
      api.instanceTypes(),
      api.regions(),
      api.filesystems(),
      api.sshKeys(),
    ])
      .then(([t, r, fs, keys]) => {
        setTypes(t);
        setRegions(r);
        setFilesystems(fs);
        setSshKeys(keys.ssh_keys);
        const defaultKey =
          keys.default && keys.ssh_keys.includes(keys.default)
            ? keys.default
            : (keys.ssh_keys[0] ?? "");
        setSshKey((v) => v || defaultKey);
        // Default to the cheapest GPU that actually has capacity.
        const firstAvailable = Object.entries(t)
          .filter(([, info]) => info.regions_with_capacity.length > 0)
          .sort((a, b) => a[1].price_usd_per_hour - b[1].price_usd_per_hour)[0];
        setInstanceType((v) => v || firstAvailable?.[0] || Object.keys(t)[0] || "");
      })
      .catch((e) => setLoadError(e.message));
  }, []);

  const selectedType = types[instanceType];
  const fsRegions = useMemo(
    () => new Set(filesystems.map((f) => f.region)),
    [filesystems],
  );

  // GPUs: available first (cheapest -> priciest), then the rest by price.
  const typeOptions = useMemo(() => {
    return Object.entries(types)
      .map(([name, t]) => ({
        name,
        t,
        available: t.regions_with_capacity.length > 0,
      }))
      .sort((a, b) => {
        if (a.available !== b.available) return a.available ? -1 : 1;
        return a.t.price_usd_per_hour - b.t.price_usd_per_hour;
      });
  }, [types]);

  // Regions: those with capacity for the chosen GPU first (a region where
  // you already have a filesystem wins ties), then the unavailable rest.
  const availableForType = useMemo(
    () => new Set(selectedType?.regions_with_capacity ?? []),
    [selectedType],
  );
  const regionOptions = useMemo(() => {
    return regions
      .map((r) => ({
        ...r,
        available: availableForType.has(r.code),
        hasFs: fsRegions.has(r.code),
      }))
      .sort((a, b) => {
        if (a.available !== b.available) return a.available ? -1 : 1;
        if (a.available && a.hasFs !== b.hasFs) return a.hasFs ? -1 : 1;
        return a.name.localeCompare(b.name);
      });
  }, [regions, availableForType, fsRegions]);

  // When the GPU changes, keep the region valid for it — preferring a region
  // where a filesystem already lives.
  useEffect(() => {
    const avail = selectedType?.regions_with_capacity ?? [];
    if (avail.length === 0) return; // out of capacity: Launch stays disabled
    if (!avail.includes(region)) {
      setRegion(avail.find((r) => fsRegions.has(r)) ?? avail[0]);
    }
  }, [instanceType, selectedType, fsRegions]); // eslint-disable-line react-hooks/exhaustive-deps

  // Filesystems are region-locked: keep the choice inside the chosen region.
  const filesystemsInRegion = useMemo(
    () => filesystems.filter((f) => f.region === region),
    [filesystems, region],
  );
  useEffect(() => {
    if (filesystem === SCRATCH_ONLY) return; // an explicit choice sticks
    if (filesystemsInRegion.some((f) => f.name === filesystem)) return;
    // Prefer a real filesystem when the region has one; otherwise fall to
    // scratch-only so a filesystem-less region is still launchable.
    setFilesystem(filesystemsInRegion[0]?.name ?? SCRATCH_ONLY);
  }, [region, filesystemsInRegion]); // eslint-disable-line react-hooks/exhaustive-deps

  const outOfCapacity =
    !!selectedType && selectedType.regions_with_capacity.length === 0;
  const scratchOnly = filesystem === SCRATCH_ONLY;
  const canLaunch =
    !!instanceType && !!region && !!filesystem && !outOfCapacity;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      await api.launch({
        instance_type: instanceType,
        region,
        filesystem: scratchOnly ? "" : filesystem,
        connection_mode: mode,
        ssh_key_name: sshKey || undefined,
        idle_timeout_seconds: idleTimeout ? parseFloat(idleTimeout) : undefined,
      });
      onLaunched();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  const field =
    "w-full rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm";

  if (loadError) {
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">
        {loadError}
      </div>
    );
  }

  return (
    <form
      onSubmit={submit}
      className="rounded-lg border border-zinc-200 bg-white p-4"
    >
      {/* GPU gets its own full-width row: price is the primary decision
          variable and must never be truncated. Each option LEADS with the
          full $/hr, then the GPU name + VRAM, so even a narrow closed control
          shows the price first. */}
      <label className="block text-xs font-medium text-zinc-600">
        1. GPU
        <select
          className={`${field} mt-1`}
          value={instanceType}
          onChange={(e) => setInstanceType(e.target.value)}
        >
          {typeOptions.map(({ name, t, available }) => (
            <option key={name} value={name} disabled={!available}>
              {formatMoney(t.price_usd_per_hour)}/hr ·{" "}
              {t.gpu_description || t.description}
              {t.specs.gpus > 1 ? ` · ${t.specs.gpus}x` : ""}
              {available ? "" : " · out of capacity"}
            </option>
          ))}
        </select>
      </label>

      <div className="mt-3 grid grid-cols-2 gap-3 md:grid-cols-4">
        <label className="block text-xs font-medium text-zinc-600">
          2. Region
          <select
            className={`${field} mt-1`}
            value={region}
            onChange={(e) => setRegion(e.target.value)}
          >
            {regionOptions.map((r) => (
              <option key={r.code} value={r.code} disabled={!r.available}>
                {r.name} ({r.code})
                {r.available
                  ? r.hasFs
                    ? " · has filesystem"
                    : ""
                  : " (not available for this type)"}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-xs font-medium text-zinc-600">
          3. Filesystem
          <select
            className={`${field} mt-1`}
            value={filesystem}
            onChange={(e) => setFilesystem(e.target.value)}
          >
            {filesystemsInRegion.map((f) => (
              <option key={f.name} value={f.name}>
                {f.name}
              </option>
            ))}
            <option value={SCRATCH_ONLY}>
              None - scratch only
              {filesystemsInRegion.length === 0
                ? " (no filesystem in this region)"
                : ""}
            </option>
          </select>
        </label>
        <label className="block text-xs font-medium text-zinc-600">
          SSH key
          <select
            className={`${field} mt-1`}
            value={sshKey}
            onChange={(e) => setSshKey(e.target.value)}
          >
            {sshKeys.length === 0 && (
              <option value="">No keys registered</option>
            )}
            {sshKeys.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-xs font-medium text-zinc-600">
          Connection
          <select
            className={`${field} mt-1`}
            value={mode}
            onChange={(e) => setMode(e.target.value)}
          >
            <option value="direct-ssh">direct-ssh</option>
            <option value="tailscale">tailscale</option>
          </select>
        </label>
        <label className="block text-xs font-medium text-zinc-600">
          Idle Timeout
          <select
            className={`${field} mt-1`}
            value={idleTimeout}
            onChange={(e) => setIdleTimeout(e.target.value)}
          >
            <option value="">Default</option>
            <option value="1800">30 min</option>
            <option value="3600">1 hour</option>
            <option value="7200">2 hours</option>
            <option value="14400">4 hours</option>
            <option value="28800">8 hours</option>
          </select>
        </label>
      </div>

      <div className="mt-3 flex items-center justify-between gap-4">
        <p className="text-xs text-zinc-500">
          {outOfCapacity ? (
            <span className="text-amber-700">
              {selectedType.description} is out of capacity everywhere right
              now. Pick another GPU, or set a capacity watch below.
            </span>
          ) : scratchOnly ? (
            <span className="text-amber-700">
              Scratch only: everything on this instance is deleted when it
              terminates. Transfer your files first, or turn on
              &ldquo;Download to this machine&rdquo; under Settings &gt; data
              safety and termination will save them automatically.
            </span>
          ) : selectedType ? (
            <span>
              <span className="font-medium text-zinc-700">
                {formatMoney(selectedType.price_usd_per_hour)}/hr
              </span>{" "}
              {selectedType.gpu_description || selectedType.description}:{" "}
              {selectedType.specs.gpus} GPU, {selectedType.specs.vcpus} vCPU,{" "}
              {selectedType.specs.memory_gib} GiB RAM
            </span>
          ) : (
            ""
          )}
        </p>
        <button
          type="submit"
          disabled={submitting || !canLaunch}
          className="rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
        >
          {submitting ? "Launching..." : "Launch"}
        </button>
      </div>

      {error && (
        <p className="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          {error}
        </p>
      )}
    </form>
  );
}
