"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ApiError,
  type Filesystem,
  type GcpQuota,
  type GcpQuotaRow,
  type InstanceTypeInfo,
  type Region,
  type Volume,
} from "@/lib/api";
import { formatMoney } from "@/lib/format";
import { HardwareGuide } from "@/components/HardwareGuide";

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
//      A launch may ALSO attach extra filesystems from the same region.
//      That stays deliberately subordinate: one filesystem is the common
//      case, and the extras row only appears once a primary is chosen.
// Under the GCP toggle step 3 is a DATA VOLUME instead (Phase 112). Same
// slot, same meaning once mounted (/lambda/nfs/<name>), three differences
// the copy has to make plain: a volume is zonal, so picking one LOCKS the
// zone above it; only one attaches per box; and a volume bills from the
// moment it exists, by provisioned size, attached or not.
// The form only collects input; every rule (region match, budget,
// concurrency, whether the disk is free) is still enforced by the backend
// and its rejection shown verbatim.

// Sentinel for "launch without a filesystem" ("" = nothing chosen yet).
const SCRATCH_ONLY = "__scratch_only__";

// How many filesystems may ride along beside the primary. The backend owns
// the real cap and refuses past it; this only keeps the form from offering
// a fifth pick that would be rejected on submit.
const MAX_EXTRA_FILESYSTEMS = 4;

// GCE's project-wide GPU cap. It counts every region at once, so it gates a
// launch no matter which zone is chosen.
const GLOBAL_GPU_METRIC = "GPUS_ALL_REGIONS";

// us-central1-a -> us-central1. A GCE zone is its region plus one trailing
// letter; anything not shaped that way (Lambda's region codes) is already a
// region and passes through untouched. Mirrors gcp_catalog.zone_to_region.
function regionOfZone(zone: string): string {
  return zone.replace(/-[a-z]$/, "");
}

// What the quota rows say about launching THIS shape here. Five outcomes,
// and keeping them apart is the whole point: "your project has room",
// "your project does not", and "Manifold could not tell" must never render
// as one another.
type QuotaGate =
  | { state: "unreadable" }                        // nothing was read at all
  | { state: "no-metric" }                         // shape is not on the shelf
  | { state: "missing-rows"; missing: string[] }   // the gating row is absent
  | { state: "blocked"; row: GcpQuotaRow; global: boolean }
  | { state: "healthy"; regional: GcpQuotaRow; globalRow: GcpQuotaRow };

// The backend returns EVERY GPU quota row Google sent, unfiltered, because
// an agent asking that question wants the whole answer. Only two of them
// can gate a launch this form produces, and these are they:
//   - the selected GPU family's regional metric (NVIDIA_T4_GPUS etc.)
//   - the global GPUS_ALL_REGIONS cap
// PREEMPTIBLE_* cannot: the GCE launch path sets no provisioning model, so
// every Manifold launch is on-demand. COMMITTED_* cannot either: Manifold
// buys no commitments, and Google fills those rows with the int64 "no limit
// configured" sentinel, which lands in JavaScript as 9223372036854776000 -
// a number that was being printed at the user. If spot launches ever ship,
// PREEMPTIBLE_* becomes gating and this is the function to change.
function readQuotaGate(
  quota: GcpQuota | null,
  type: InstanceTypeInfo | undefined,
): QuotaGate {
  if (!quota || !type) return { state: "unreadable" };
  const need = type.specs.gpus;
  const metric = type.quota_metric;
  if (!metric) return { state: "no-metric" };
  const globalRow = quota.quotas.find(
    (q) => q.metric === GLOBAL_GPU_METRIC && q.scope === "global",
  );
  // One region's rows come back per request, so scope alone identifies the
  // regional row - and the row's own scope is the region name to print,
  // rather than one this form derived.
  const regional = quota.quotas.find(
    (q) => q.metric === metric && q.scope !== "global",
  );
  // A multi-GPU shape needs its whole count: a2-highgpu-8g wants 8, and
  // "limit above zero" says nothing about whether 8 are free.
  if (globalRow && globalRow.limit - globalRow.usage < need)
    return { state: "blocked", row: globalRow, global: true };
  if (regional && regional.limit - regional.usage < need)
    return { state: "blocked", row: regional, global: false };
  if (!globalRow || !regional)
    return {
      state: "missing-rows",
      missing: [
        ...(globalRow ? [] : [GLOBAL_GPU_METRIC]),
        ...(regional ? [] : [metric]),
      ],
    };
  return { state: "healthy", regional, globalRow };
}

// Google's own numbers, labelled as the snapshot they are - never rounded,
// never restated as a percentage, never turned into a verdict.
function quotaSnapshot(row: GcpQuotaRow): string {
  const where = row.scope === "global" ? "" : ` in ${row.scope}`;
  // A limit of zero is the fresh-project case: "0 of 0 in use" is true but
  // says the wrong thing about why there is no room.
  if (row.limit <= 0) return `${row.metric}${where} is 0 as of this check`;
  return `${row.metric}${where} is ${row.usage} of ${row.limit} in use as of this check`;
}

// Why this volume cannot be attached right now, or null when it can.
// Rendered on a DISABLED option rather than hidden: a volume missing from
// the list reads as "you do not have one", and both reasons below are
// things the user can act on (terminate the holder; create one here).
function volumeBlocker(v: Volume): string | null {
  if (v.attached_to.length > 0) return `in use by ${v.attached_to[0]}`;
  if (!v.known_to_manifold) return "not created by Manifold";
  return null;
}

const QUOTA_AMBER =
  "rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] leading-relaxed text-amber-800";
const QUOTA_NEUTRAL =
  "rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-[11px] leading-relaxed text-zinc-600";

/** The GCP quota note under the provider toggle.
 *
 * It reports what Google said and nothing else. It never predicts the
 * launch in either direction: not "this will fail" (an instance
 * terminating right now hands the quota back), and not "this will
 * succeed" (quota is permission to ask - RESOURCE_POOL_EXHAUSTED is a
 * separate answer Google gives at insert time, with quota to spare).
 */
function GcpQuotaNote({
  gate,
  failed,
  typeName,
  need,
  requestUrl,
}: {
  gate: QuotaGate;
  failed: boolean;
  typeName: string;
  need: number;
  requestUrl?: string;
}) {
  // No project id, no link: a quotas URL without one opens whichever
  // project the browser used last, which is not the one being described.
  const consoleLink = (label: string) =>
    requestUrl ? (
      <a className="underline" href={requestUrl} target="_blank" rel="noreferrer">
        {label}
      </a>
    ) : (
      <span>{label} in the Google Cloud console</span>
    );
  const answerTime = " Google usually answers small requests in minutes to hours.";

  if (gate.state === "unreadable") {
    // Silence unless the read actually failed. Amber here was the original
    // bug: an unasked question rendered as a problem with the project.
    if (!failed) return null;
    return (
      <p className={QUOTA_NEUTRAL}>
        Manifold could not read this project&rsquo;s GPU quota just now, so it
        is not saying anything about it either way. Google still checks quota
        when it creates the machine.
      </p>
    );
  }

  if (gate.state === "no-metric") {
    return (
      <p className={QUOTA_NEUTRAL}>
        Manifold does not know which GPU quota gates {typeName}, so it cannot
        say what this project holds for it. {consoleLink("See your quotas")}.
      </p>
    );
  }

  if (gate.state === "missing-rows") {
    return (
      <p className={QUOTA_NEUTRAL}>
        Google&rsquo;s quota answer did not include{" "}
        {gate.missing.join(" or ")}, so Manifold cannot say whether there is
        room for {typeName}. The number is missing, not zero.{" "}
        {consoleLink("See your quotas")}.
      </p>
    );
  }

  // Usage above zero is the remedy nobody thinks of: some of it is very
  // likely this project's own running instances, and terminating one frees
  // the same room a bigger quota would. Said only when there IS usage -
  // offering it against a limit of zero would be advice that does nothing.
  const usageIsYours = (row: GcpQuotaRow) =>
    row.usage > 0 ? (
      <>
        {" "}
        That usage can be your own running instances - terminating one, or
        waiting for it to finish, hands the room back.
      </>
    ) : null;

  if (gate.state === "blocked" && gate.global) {
    return (
      <p className={QUOTA_AMBER}>
        Your project&rsquo;s global GPU cap: {quotaSnapshot(gate.row)}, and{" "}
        {typeName} needs {need}. That cap counts every region at once, so
        another region does not get around it.{usageIsYours(gate.row)}{" "}
        {consoleLink("Request an increase")}.{answerTime}
      </p>
    );
  }

  if (gate.state === "blocked") {
    return (
      <p className={QUOTA_AMBER}>
        {quotaSnapshot(gate.row)}, and {typeName} needs {need}. GPU quota is
        per region, so another region has its own.{usageIsYours(gate.row)}{" "}
        {consoleLink("Request an increase")}.{answerTime}
      </p>
    );
  }

  return (
    <p className={QUOTA_NEUTRAL}>
      {gate.regional.metric} in {gate.regional.scope}: {gate.regional.usage} of{" "}
      {gate.regional.limit} in use as of this check, and {gate.globalRow.usage}{" "}
      of {gate.globalRow.limit} against the global cap. Room for the {need}{" "}
      {typeName} needs - room to ask, not a promise that Google has one free
      in this zone. {consoleLink("Request an increase")}.
    </p>
  );
}

export function LaunchForm({ onLaunched }: { onLaunched: () => void }) {
  const [types, setTypes] = useState<Record<string, InstanceTypeInfo>>({});
  const [regions, setRegions] = useState<Region[]>([]);
  const [filesystems, setFilesystems] = useState<Filesystem[]>([]);
  const [sshKeys, setSshKeys] = useState<string[]>([]);
  const [instanceType, setInstanceType] = useState("");
  const [region, setRegion] = useState("");
  const [filesystem, setFilesystem] = useState("");
  const [extraFilesystems, setExtraFilesystems] = useState<string[]>([]);
  const [sshKey, setSshKey] = useState("");
  const [mode, setMode] = useState("direct-ssh");
  const [idleTimeout, setIdleTimeout] = useState("");
  const [maxLifetime, setMaxLifetime] = useState("");
  const [maxActive, setMaxActive] = useState("");
  const [purpose, setPurpose] = useState("");
  const [bootstrap, setBootstrap] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [provider, setProvider] = useState("lambda");
  // Whether a human has picked a provider on this form. A click always
  // wins: the account default below only SEEDS the tab, and it arrives
  // asynchronously, so without this a preference landing a moment after
  // the click would silently move the launch to another cloud.
  const providerPicked = useRef(false);
  const [volumes, setVolumes] = useState<Volume[]>([]);
  // A volume read that FAILED is not an empty list of volumes. Kept apart so
  // the picker can say "could not ask Google" instead of "you have none".
  const [volumesFailed, setVolumesFailed] = useState(false);
  const [gcpQuota, setGcpQuota] = useState<GcpQuota | null>(null);
  // A failed read is its OWN state, never null-and-silent and never zero:
  // "nobody could ask Google" and "Google said none" are different answers
  // and the form must be able to tell them apart.
  const [gcpQuotaFailed, setGcpQuotaFailed] = useState(false);
  const [loadError, setLoadError] = useState("");

  useEffect(() => {
    Promise.all([
      api.instanceTypes(provider),
      api.regions(provider),
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
        setInstanceType(firstAvailable?.[0] || Object.keys(t)[0] || "");
      })
      .catch((e) => setLoadError(e.message));
  }, [provider]);

  // The tab opens on the account's default provider (Settings -> Default
  // provider), so the form agrees with what an agent launching right now
  // would get. The form always SENDS the provider explicitly, so what is
  // on screen is what launches, whatever the default does later.
  useEffect(() => {
    let cancelled = false;
    api
      .preferences()
      .then((r) => {
        const preferred = r.preferences.providers.default_provider;
        if (cancelled || providerPicked.current || !preferred) return;
        setProvider(preferred);
      })
      .catch(() => {
        // A preferences read that fails leaves the tab where it is. The
        // launch still carries whatever the tab shows, so nothing is
        // silently redirected.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Volumes are GCP-only and, like the quota read, kept off the catalog
  // load so a slow or unconfigured project cannot take the form down.
  useEffect(() => {
    if (provider !== "gcp") {
      setVolumes([]);
      setVolumesFailed(false);
      return;
    }
    let cancelled = false;
    api
      .volumes()
      .then((r) => {
        if (cancelled) return;
        setVolumes(r.volumes);
        setVolumesFailed(false);
      })
      .catch(() => {
        if (cancelled) return;
        setVolumes([]);
        setVolumesFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [provider]);

  // The GCP quota read is separate from the catalog load: it can be slow or
  // 503 (no ADC yet) without taking the form down, and it only matters
  // under the GCP toggle.
  useEffect(() => {
    if (provider !== "gcp") {
      setGcpQuota(null);
      setGcpQuotaFailed(false);
      return;
    }
    api
      .gcpQuota(region || undefined)
      .then((q) => {
        setGcpQuota(q);
        setGcpQuotaFailed(false);
      })
      .catch(() => {
        setGcpQuota(null);
        setGcpQuotaFailed(true);
      });
  }, [provider, region]);

  const selectedType = types[instanceType];
  const fsRegions = useMemo(
    () => new Set(filesystems.map((f) => f.region)),
    [filesystems],
  );

  // Step 3 is provider-scoped. On Lambda it is a filesystem, filtered BY the
  // chosen region. On GCP it is a data volume, which MOVES the chosen zone -
  // the opposite direction, and why it gets its own effects below rather
  // than a branch inside the filesystem ones.
  const isGcp = provider === "gcp";
  const selectedVolume = useMemo(
    () => (isGcp ? (volumes.find((v) => v.name === filesystem) ?? null) : null),
    [isGcp, volumes, filesystem],
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
  // where a filesystem already lives, then the region the listed prices are
  // quoted in.
  //
  // That second step is GCP's: its zones arrive alphabetically, so the raw
  // first pick was asia-east1-a sitting beside a us-central1 price. It keys
  // on price_basis_region, a field only GCP entries carry, so Lambda keeps
  // exactly the behaviour it had. Nothing is hidden either way: the default
  // only ever picks from regions_with_capacity, and the dropdown still
  // lists every region.
  useEffect(() => {
    // A chosen volume owns the zone. Moving it here to a zone with capacity
    // would silently unpin the volume and produce a launch the backend
    // refuses; the zone-capacity blocker below says so instead.
    if (selectedVolume) return;
    const avail = selectedType?.regions_with_capacity ?? [];
    if (avail.length === 0) return; // out of capacity: Launch stays disabled
    if (!avail.includes(region)) {
      const priced = selectedType?.price_basis_region;
      setRegion(
        avail.find((r) => fsRegions.has(r)) ??
          (priced
            ? avail.find((r) => regionOfZone(r) === priced)
            : undefined) ??
          avail[0],
      );
    }
  }, [instanceType, selectedType, fsRegions, selectedVolume]); // eslint-disable-line react-hooks/exhaustive-deps

  // A volume is ZONAL, so choosing one IS choosing the zone. Stated as its
  // own effect because it runs the other way round from every filesystem
  // rule on this form: the choice sets the zone instead of being filtered
  // by it.
  useEffect(() => {
    if (selectedVolume && region !== selectedVolume.zone) {
      setRegion(selectedVolume.zone);
    }
  }, [selectedVolume]); // eslint-disable-line react-hooks/exhaustive-deps

  // Filesystems are region-locked: keep the choice inside the chosen region.
  const filesystemsInRegion = useMemo(
    () => filesystems.filter((f) => f.region === region),
    [filesystems, region],
  );
  useEffect(() => {
    if (isGcp) {
      // Volumes are not filtered by region (they set it), so the only thing
      // to repair here is a name that does not exist on this cloud: a
      // Lambda filesystem left behind by the provider toggle, or a volume
      // deleted since the list was read.
      if (filesystem === SCRATCH_ONLY) return;
      if (volumes.some((v) => v.name === filesystem)) return;
      setFilesystem(SCRATCH_ONLY);
      return;
    }
    if (filesystem === SCRATCH_ONLY) return; // an explicit choice sticks
    if (filesystemsInRegion.some((f) => f.name === filesystem)) return;
    // Prefer a real filesystem when the region has one; otherwise fall to
    // scratch-only so a filesystem-less region is still launchable.
    setFilesystem(filesystemsInRegion[0]?.name ?? SCRATCH_ONLY);
  }, [region, filesystemsInRegion, isGcp, volumes]); // eslint-disable-line react-hooks/exhaustive-deps

  // Extras are region-locked too, and an extra can never be the primary.
  // Whenever either changes, drop the ones that no longer make sense rather
  // than sending a name the backend will refuse.
  useEffect(() => {
    setExtraFilesystems((current) =>
      current.filter(
        (n) =>
          n !== filesystem && filesystemsInRegion.some((f) => f.name === n),
      ),
    );
  }, [filesystem, filesystemsInRegion]);

  const attachableFilesystems = useMemo(
    () =>
      filesystemsInRegion.filter(
        (f) => f.name !== filesystem && !extraFilesystems.includes(f.name),
      ),
    [filesystemsInRegion, filesystem, extraFilesystems],
  );

  // Read against the SELECTED GPU (its whole GPU count) and the selected
  // zone's region - a quota answer about some other shape or some other
  // place is not an answer about this launch.
  const quotaGate = useMemo(
    () => readQuotaGate(gcpQuota, selectedType),
    [gcpQuota, selectedType],
  );

  // The listed prices are one region's. When the chosen zone is somewhere
  // else, say so beside them - the price still shows, because relative
  // price is how a GPU gets picked and the GPU is picked before a region
  // exists. Manifold does not have the other region's rate, and will not
  // invent a multiplier for it.
  const pricedRegion = selectedType?.price_basis_region;
  const priceRegionMismatch =
    !!pricedRegion && !!region && regionOfZone(region) !== pricedRegion;

  const outOfCapacity =
    !!selectedType && selectedType.regions_with_capacity.length === 0;
  const scratchOnly = filesystem === SCRATCH_ONLY;
  // The zone trap, and the reason the docs lead with it: a volume can only
  // attach in its own zone, and GPU capacity varies by zone - so data can
  // sit in a zone where the GPU you want is unavailable. Neither side is
  // moved silently; the user picks which one to change.
  const volumeZoneHasNoCapacity =
    !!selectedVolume &&
    !!selectedType &&
    !selectedType.regions_with_capacity.includes(selectedVolume.zone);
  const canLaunch =
    !!instanceType &&
    !!region &&
    !!filesystem &&
    !outOfCapacity &&
    !volumeZoneHasNoCapacity;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      await api.launch({
        provider,
        instance_type: instanceType,
        region,
        filesystem: scratchOnly ? "" : filesystem,
        // Extras only travel with a primary; the backend refuses them
        // without one, so the form never sends that combination.
        extra_filesystems:
          scratchOnly || extraFilesystems.length === 0
            ? undefined
            : extraFilesystems,
        connection_mode: mode,
        ssh_key_name: sshKey || undefined,
        idle_timeout_seconds: idleTimeout ? parseFloat(idleTimeout) : undefined,
        max_lifetime_seconds: maxLifetime ? parseFloat(maxLifetime) : undefined,
        max_active_seconds: maxActive ? parseFloat(maxActive) : undefined,
        purpose: purpose.trim() || undefined,
        // Sent verbatim (it is code), but a blank one is not sent at all.
        bootstrap: bootstrap.trim() ? bootstrap : undefined,
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
      <div className="mb-4 flex items-center gap-4 border-b border-zinc-200 pb-4">
        <label className="text-xs font-medium text-zinc-600 mr-2">Provider</label>
        <div className="flex bg-zinc-100 p-1 rounded-md">
          <button
            type="button"
            className={`px-3 py-1 text-xs font-medium rounded-sm ${provider === "lambda" ? "bg-white shadow-sm text-zinc-900" : "text-zinc-500 hover:text-zinc-700"}`}
            onClick={() => {
              providerPicked.current = true;
              setProvider("lambda");
            }}
          >
            Lambda AI
          </button>
          <button
            type="button"
            className={`px-3 py-1 text-xs font-medium rounded-sm ${provider === "gcp" ? "bg-white shadow-sm text-zinc-900" : "text-zinc-500 hover:text-zinc-700"}`}
            onClick={() => {
              providerPicked.current = true;
              setProvider("gcp");
            }}
          >
            Google Cloud
          </button>
        </div>
      </div>

      {provider === "gcp" && Object.keys(types).length > 0 && (
        <div className="mb-4 space-y-2">
          <p className={QUOTA_NEUTRAL}>
            {Object.values(types).find((t) => t.price_basis)?.price_basis}
            {priceRegionMismatch ? (
              <>
                {" "}You have {regionOfZone(region)} selected, and Google
                prices per region - Manifold does not have that
                region&rsquo;s rate.
              </>
            ) : null}
            {" "}A data volume gives a GCP box a persistent home at
            /lambda/nfs/&lt;name&gt;; without one everything on the instance
            is deleted when it terminates.
          </p>
          {volumesFailed ? (
            <p className={QUOTA_NEUTRAL}>
              Manifold could not read this project&rsquo;s data volumes just
              now, so it is not saying you have none - it is saying it could
              not ask. Launching without one is still scratch-only.
            </p>
          ) : null}
          <GcpQuotaNote
            gate={quotaGate}
            failed={gcpQuotaFailed}
            typeName={instanceType}
            need={selectedType?.specs.gpus ?? 0}
            requestUrl={gcpQuota?.request_url}
          />
        </div>
      )}

      {provider === "gcp" && Object.keys(types).length === 0 && (
        <p className="mb-4 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          Google Cloud is not wired up yet: Manifold cannot list, launch, or
          bill GCP machines, so there is nothing to show here. This toggle
          exists so the provider seam is real and testable. Lambda is the
          working provider.
        </p>
      )}

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
        <HardwareGuide
          current={instanceType}
          onPick={(t) => setInstanceType(t)}
          provider={provider}
        />
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
          {isGcp ? "3. Data volume" : "3. Filesystem"}
          <select
            className={`${field} mt-1`}
            value={filesystem}
            onChange={(e) => setFilesystem(e.target.value)}
          >
            {isGcp
              ? volumes.map((v) => {
                  const blocked = volumeBlocker(v);
                  return (
                    <option key={v.name} value={v.name} disabled={!!blocked}>
                      {v.name} · {v.size_gb} GiB · {v.zone}
                      {blocked ? ` · ${blocked}` : ""}
                    </option>
                  );
                })
              : filesystemsInRegion.map((f) => (
                  <option key={f.name} value={f.name}>
                    {f.name}
                  </option>
                ))}
            <option value={SCRATCH_ONLY}>
              None - scratch only
              {isGcp
                ? volumes.length === 0 && !volumesFailed
                  ? " (no volumes in this project)"
                  : ""
                : filesystemsInRegion.length === 0
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
        <label className="block text-xs font-medium text-zinc-600">
          Max lifetime
          <select
            className={`${field} mt-1`}
            value={maxLifetime}
            onChange={(e) => setMaxLifetime(e.target.value)}
          >
            <option value="">None</option>
            <option value="7200">2 hours</option>
            <option value="14400">4 hours</option>
            <option value="28800">8 hours</option>
            <option value="86400">24 hours</option>
            <option value="259200">3 days</option>
          </select>
        </label>
        <label className="block text-xs font-medium text-zinc-600">
          Max active time
          <select
            className={`${field} mt-1`}
            value={maxActive}
            onChange={(e) => setMaxActive(e.target.value)}
          >
            <option value="">None</option>
            <option value="3600">1 hour</option>
            <option value="7200">2 hours</option>
            <option value="14400">4 hours</option>
            <option value="28800">8 hours</option>
            <option value="86400">24 hours</option>
          </select>
          <span className="mt-1 block text-[11px] font-normal text-zinc-400">
            Counted from when the instance becomes active; boot never
            spends it. Max lifetime above stays the outer bound.
          </span>
        </label>
        <label className="block text-xs font-medium text-zinc-600">
          Purpose
          <input
            className={`${field} mt-1`}
            placeholder='e.g. "fine-tune batch for the crop set"'
            maxLength={200}
            value={purpose}
            onChange={(e) => setPurpose(e.target.value)}
          />
          <span className="mt-1 block text-[11px] font-normal text-zinc-400">
            Shown wherever this instance is listed. It is how agents and
            other sessions know not to touch your box.
          </span>
        </label>
      </div>

      {/* The three things about a volume that are not true of a filesystem,
          said where the choice is made rather than in a doc. */}
      {selectedVolume ? (
        <p className="mt-2 text-[11px] text-zinc-500">
          {selectedVolume.name} mounts at {selectedVolume.mount_point} and
          survives this instance. It is zonal, so the zone above is locked to{" "}
          {selectedVolume.zone}; only one volume attaches per instance; and it
          bills for its full {selectedVolume.size_gb} GiB whether an instance
          is attached or not (about $
          {selectedVolume.list_price_usd_per_month.toFixed(2)}/month at
          Google&rsquo;s list price).
          {selectedVolume.formatted_at
            ? ""
            : " Manifold formats it on first use, before this launch reports ready."}
        </p>
      ) : null}

      {/* Extra filesystems: deliberately quiet and below the main grid. One
          filesystem is the normal launch; this row exists for the run that
          reads one dataset and writes another. */}
      {filesystem &&
      !scratchOnly &&
      (extraFilesystems.length > 0 || attachableFilesystems.length > 0) ? (
        <div className="mt-3 border-t border-zinc-100 pt-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium text-zinc-600">
              Also mounted
            </span>
            {extraFilesystems.map((name) => (
              <span
                key={name}
                className="flex items-center gap-1 rounded-full border border-zinc-200 bg-zinc-100 px-2 py-0.5 text-[11px] text-zinc-700"
              >
                {name}
                <button
                  type="button"
                  className="text-zinc-400 hover:text-zinc-700"
                  aria-label={`Do not attach ${name}`}
                  onClick={() =>
                    setExtraFilesystems((current) =>
                      current.filter((n) => n !== name),
                    )
                  }
                >
                  ×
                </button>
              </span>
            ))}
            {extraFilesystems.length === 0 && (
              <span className="text-[11px] text-zinc-400">
                just {filesystem}
              </span>
            )}
            {attachableFilesystems.length > 0 &&
            extraFilesystems.length < MAX_EXTRA_FILESYSTEMS ? (
              <select
                className="rounded border border-zinc-300 bg-white px-2 py-1 text-[11px] text-zinc-600"
                value=""
                onChange={(e) => {
                  const picked = e.target.value;
                  if (!picked) return;
                  setExtraFilesystems((current) => [...current, picked]);
                }}
              >
                <option value="">+ attach another filesystem</option>
                {attachableFilesystems.map((f) => (
                  <option key={f.name} value={f.name}>
                    {f.name}
                  </option>
                ))}
              </select>
            ) : null}
          </div>
          <p className="mt-1 text-[11px] text-zinc-400">
            {extraFilesystems.length >= MAX_EXTRA_FILESYSTEMS
              ? `${MAX_EXTRA_FILESYSTEMS} extra filesystems is the limit. `
              : ""}
            Extras mount at /lambda/nfs/&lt;name&gt; and are yours to read and
            write from the terminal, a command, or the Files browser. Jobs
            still use {filesystem}, the filesystem chosen above. Filesystems
            attach only at launch, so pick them now.
          </p>
        </div>
      ) : null}
      {/* Collapsed by default: an empty bootstrap is the normal case, and a
          textarea sitting open above the Launch button would read as
          something you are meant to fill in. */}
      <details className="mt-3" open={!!bootstrap}>
        <summary className="cursor-pointer text-xs font-medium text-zinc-600">
          Bootstrap script (optional)
        </summary>
        <textarea
          className={`${field} mt-2 font-mono`}
          rows={6}
          maxLength={16384}
          placeholder={"git clone https://github.com/me/project ~/project\ncd ~/project && pip install -r requirements.txt"}
          value={bootstrap}
          onChange={(e) => setBootstrap(e.target.value)}
        />
        <p className="mt-1 text-[11px] text-zinc-500">
          Bash, run once on the instance when it comes up. It keeps running
          if you close this page or restart Manifold, and the instance
          counts as busy while it works, so the idle timeout will not reap
          it mid-install. If the script fails, the instance is left running
          and you get a notification - nothing is destroyed over a bad setup
          line. Up to 16 KiB.
        </p>
      </details>

      {maxLifetime ? (
        <p className="mt-2 text-xs text-zinc-500">
          Maximum total lifetime, from launch acceptance - includes boot (5-40
          min). Unlike the idle timeout, nothing on the instance can push this
          out: it applies while a model is being served and it survives a
          backend restart. Manifold terminates the instance at that point if it
          can reach it and save its files first.
        </p>
      ) : null}

      <div className="mt-3 flex items-center justify-between gap-4">
        <p className="text-xs text-zinc-500">
          {outOfCapacity ? (
            <span className="text-amber-700">
              {selectedType.description} is out of capacity everywhere right
              now. Pick another GPU, or set a capacity watch below.
            </span>
          ) : volumeZoneHasNoCapacity ? (
            <span className="text-amber-700">
              {selectedVolume!.name} lives in {selectedVolume!.zone}, and{" "}
              {instanceType} is not available there. A volume can only attach
              in its own zone, so pick a GPU this zone has, or launch without
              the volume - Manifold will not quietly move the zone and leave
              your data behind.
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
