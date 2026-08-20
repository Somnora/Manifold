"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type Region, type Volume } from "@/lib/api";

// GCP data volumes: create, see, delete. Deliberately NOT a browser.
//
// A Lambda filesystem can be browsed with no instance running, because it
// is backed by an S3-compatible API. A Persistent Disk cannot: nothing
// outside the instance can read it, which is also why there is no
// bytes_used column here and never a "0 GiB used". What a detached disk CAN
// tell you is its provisioned size, its zone, and who is holding it - and
// those are exactly the three things that decide a launch.
//
// Three facts this panel exists to put in front of someone before they
// create one: a volume is zonal (so it pins where you can launch), it
// attaches to one instance at a time, and it bills for its provisioned size
// from creation whether anything is attached or not.

const DEFAULT_SIZE_GB = 200;

// What state this volume is in, in the words a reader needs. Never
// collapsed into a single "ready" - "attached to a box" and "Manifold has
// no record of it" lead to completely different next actions.
function volumeState(v: Volume): string {
  if (v.attached_to.length > 0) return `attached to ${v.attached_to[0]}`;
  if (!v.known_to_manifold) return "not created by Manifold - unusable here";
  if (!v.formatted_at) return "new - formatted on first launch";
  return `ready (${v.fstype ?? "formatted"})`;
}

export function VolumesPanel() {
  const [volumes, setVolumes] = useState<Volume[]>([]);
  const [priceBasis, setPriceBasis] = useState("");
  // "could not read" is its own state, never an empty list: a disk bills
  // whether or not anyone could list it, so silence here must not read as
  // "you have none".
  const [loadError, setLoadError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [zones, setZones] = useState<Region[]>([]);
  const [newName, setNewName] = useState("");
  const [newZone, setNewZone] = useState("");
  const [newSize, setNewSize] = useState(String(DEFAULT_SIZE_GB));
  const [creating, setCreating] = useState(false);
  const [note, setNote] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [deleteTyped, setDeleteTyped] = useState("");
  const [deleting, setDeleting] = useState(false);

  function reload() {
    api
      .volumes()
      .then((r) => {
        setVolumes(r.volumes);
        setPriceBasis(r.price_basis);
        setLoadError("");
      })
      .catch((e) => setLoadError(e instanceof ApiError ? e.message : String(e)))
      .finally(() => setLoaded(true));
  }

  useEffect(() => {
    reload();
    // The zone list is the GCP catalog's own: a volume in a zone with no
    // GPUs on the shelf is data you cannot reach from a GPU.
    api
      .regions("gcp")
      .then((rs) => {
        setZones(rs);
        setNewZone((v) => v || (rs[0]?.code ?? ""));
      })
      .catch(() => {});
  }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    const size = parseInt(newSize, 10);
    if (!newName.trim() || !newZone || !Number.isFinite(size)) return;
    setCreating(true);
    setNote("");
    try {
      const v = await api.createVolume(newName.trim(), newZone, size);
      setNewName("");
      setNote(
        `Created ${v.name}: ${v.size_gb} GiB in ${v.zone}. It is billing from ` +
          `now, and it is formatted the first time a launch attaches it.`,
      );
      reload();
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  }

  async function remove() {
    if (!deleteTarget || deleteTyped !== deleteTarget) return;
    setDeleting(true);
    setNote("");
    try {
      const r = await api.deleteVolume(deleteTarget, deleteTyped);
      setNote(`Deleted ${r.deleted} (${r.size_gb} GiB in ${r.zone}).`);
      setDeleteTarget(null);
      setDeleteTyped("");
      reload();
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : String(err));
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="space-y-3 rounded-lg border border-zinc-200 bg-white p-4">
      <div>
        <h2 className="text-sm font-medium text-zinc-800">
          Google Cloud data volumes
        </h2>
        <p className="mt-1 text-xs text-zinc-500">
          A persistent disk for a GCP instance, mounted at
          /lambda/nfs/&lt;name&gt; and surviving the box it is attached to.
          Zonal: it can only attach in its own zone, and GPU capacity varies
          by zone, so pick the zone you actually launch in. One volume per
          instance. Unlike a Lambda filebase it cannot be browsed from here -
          nothing outside the instance can read a detached disk.
        </p>
      </div>

      {loadError ? (
        <p className="rounded border border-zinc-200 bg-zinc-50 px-3 py-2 text-xs text-zinc-600">
          {loadError}
        </p>
      ) : null}

      <div className="overflow-x-auto rounded border border-zinc-200">
        <table className="w-full text-sm">
          <thead className="bg-zinc-50 text-left text-xs uppercase tracking-wide text-zinc-500">
            <tr>
              <th className="px-3 py-2 font-medium">Name</th>
              <th className="px-3 py-2 font-medium">Zone</th>
              <th className="px-3 py-2 font-medium">Provisioned</th>
              <th className="px-3 py-2 font-medium">List price</th>
              <th className="px-3 py-2 font-medium">State</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100">
            {volumes.map((v) => (
              <tr key={v.name}>
                <td className="break-all px-3 py-2 font-mono text-xs">
                  {v.name}
                </td>
                <td className="px-3 py-2 whitespace-nowrap text-zinc-600">
                  {v.zone}
                </td>
                <td className="px-3 py-2 whitespace-nowrap text-zinc-600">
                  {v.size_gb} GiB
                </td>
                <td className="px-3 py-2 whitespace-nowrap text-zinc-600">
                  ${v.list_price_usd_per_month.toFixed(2)}/mo
                </td>
                <td className="px-3 py-2 text-zinc-600">{volumeState(v)}</td>
                <td className="px-3 py-2 text-right">
                  <button
                    onClick={() => {
                      setDeleteTarget(v.name);
                      setDeleteTyped("");
                      setNote("");
                    }}
                    disabled={v.attached_to.length > 0}
                    title={
                      v.attached_to.length > 0
                        ? "Attached to an instance; terminate it first"
                        : `Delete ${v.name} and everything on it`
                    }
                    className="rounded border border-red-200 px-2 py-0.5 text-xs text-red-700 hover:bg-red-50 disabled:opacity-40"
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
            {volumes.length === 0 && (
              <tr>
                <td
                  colSpan={6}
                  className="px-3 py-6 text-center text-sm text-zinc-500"
                >
                  {/* Loading, empty, and unreadable are different facts and
                      only one of them is bad news. */}
                  {!loaded
                    ? "Loading volumes..."
                    : loadError
                      ? "The volume list is unknown - not necessarily empty."
                      : "No data volumes yet."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {deleteTarget ? (
        <div className="flex flex-wrap items-end gap-3 rounded border border-red-200 bg-red-50 p-3">
          <label className="block text-xs font-medium text-red-700">
            Type {deleteTarget} to confirm. This destroys the whole disk;
            Manifold cannot read a detached disk, so it cannot tell you what
            is on it first.
            <input
              autoFocus
              className="mt-1 block w-64 rounded border border-red-300 bg-white px-2.5 py-1.5 font-mono text-sm"
              value={deleteTyped}
              onChange={(e) => setDeleteTyped(e.target.value)}
            />
          </label>
          <button
            onClick={remove}
            disabled={deleting || deleteTyped !== deleteTarget}
            className="rounded bg-red-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-40"
          >
            {deleting ? "Deleting..." : "Delete forever"}
          </button>
          <button
            onClick={() => setDeleteTarget(null)}
            className="rounded border border-zinc-300 px-3 py-1.5 text-sm text-zinc-600 hover:bg-zinc-50"
          >
            Cancel
          </button>
        </div>
      ) : null}

      <form onSubmit={create} className="flex flex-wrap items-end gap-3">
        <label className="block text-xs font-medium text-zinc-600">
          New volume
          <input
            className="mt-1 block rounded border border-zinc-300 bg-white px-2.5 py-1.5 font-mono text-sm"
            placeholder="e.g. crop-archive"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
          />
        </label>
        <label className="block text-xs font-medium text-zinc-600">
          Zone
          <select
            className="mt-1 block rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
            value={newZone}
            onChange={(e) => setNewZone(e.target.value)}
          >
            {zones.length === 0 && <option value="">No zones available</option>}
            {zones.map((z) => (
              <option key={z.code} value={z.code}>
                {z.code}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-xs font-medium text-zinc-600">
          Size (GiB)
          <input
            type="number"
            min={10}
            max={4096}
            className="mt-1 block w-24 rounded border border-zinc-300 bg-white px-2.5 py-1.5 text-sm"
            value={newSize}
            onChange={(e) => setNewSize(e.target.value)}
          />
        </label>
        <button
          type="submit"
          disabled={creating || !newName.trim() || !newZone}
          className="rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
        >
          {creating ? "Creating..." : "Create"}
        </button>
        <p className="pb-1.5 text-xs text-zinc-500">
          Lowercase letters, digits and dashes; 2-63 characters. Billing
          starts at creation, by provisioned size, attached or not.
          {priceBasis ? ` ${priceBasis}` : ""}
        </p>
      </form>

      {note && <p className="text-xs text-zinc-600">{note}</p>}
    </div>
  );
}
