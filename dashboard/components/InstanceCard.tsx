"use client";

import { useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  type Instance,
  type RescueReport,
  type UnpersistedFile,
} from "@/lib/api";
import { StatusBadge } from "@/components/Badge";
import { TelemetryChart } from "@/components/TelemetryChart";
import { useTerminalDock } from "@/components/TerminalDock";
import { formatBytes, formatMoney } from "@/lib/format";

export function InstanceCard({
  instance,
  onChanged,
}: {
  instance: Instance;
  onChanged: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  // When the dock (or a narrow window) squeezes the card, the action row
  // used to push its buttons off the page. Below the needed width the dock
  // buttons collapse into a ">>" menu; Terminate always stays visible.
  // Hysteresis: remember the width the full row NEEDED when it overflowed,
  // and only expand again once that much room is back (no flicker).
  const actionsRef = useRef<HTMLDivElement>(null);
  const neededWidth = useRef(0);
  const [collapsed, setCollapsed] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  useEffect(() => {
    const row = actionsRef.current;
    if (!row) return;
    const check = () => {
      if (!collapsed && row.scrollWidth > row.clientWidth + 1) {
        neededWidth.current = row.scrollWidth;
        setCollapsed(true);
      } else if (collapsed && row.clientWidth >= neededWidth.current) {
        setCollapsed(false);
        setMenuOpen(false);
      }
    };
    check();
    const obs = new ResizeObserver(check);
    obs.observe(row);
    return () => obs.disconnect();
  }, [collapsed]);
  // Terminal / Chat / Files / Browse all open in the DOCK (snappable bottom
  // or right, tabs or split) instead of unrolling inside this card.
  const { dockInstance, dockPanel } = useTerminalDock();
  const [renaming, setRenaming] = useState(false);
  const [newName, setNewName] = useState("");
  const [editingTimeout, setEditingTimeout] = useState(false);
  const [newTimeout, setNewTimeout] = useState("");
  const [busy, setBusy] = useState<"" | "terminating" | "rescuing">("");
  // Set when termination was REFUSED: the rescue ran and some file still
  // could not be saved. `blockedRescue` says what it did manage to save.
  const [blockedFiles, setBlockedFiles] = useState<UnpersistedFile[] | null>(
    null,
  );
  const [blockedRescue, setBlockedRescue] = useState<RescueReport | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [ideInfo, setIdeInfo] = useState<{
    vscode_url: string;
    cursor_url: string;
    ssh_alias: string;
    ssh_command: string;
  } | null>(null);
  const [attachingIde, setAttachingIde] = useState(false);
  const [copiedCmd, setCopiedCmd] = useState(false);

  // Latch "has been connected". The SSH supervisor can briefly flip to
  // reconnecting when the box is saturated (e.g. downloading a 15GB model),
  // and gating the action buttons/panels on the LIVE state made the whole
  // UI — terminal included — disappear and reappear on every blip. Once an
  // instance has connected, keep the controls mounted; each panel shows its
  // own connection status. The card leaves entirely when the instance is
  // terminated (it drops out of the list), so nothing lingers.
  const connected = instance.connection_state === "connected";
  const [everConnected, setEverConnected] = useState(false);
  useEffect(() => {
    if (connected) setEverConnected(true);
  }, [connected]);

  // Termination saves the instance's scratch files first (per the data-safety
  // policy in Settings), then stops the billing. It only refuses if a file
  // could NOT be saved — and then it says which, and what it did save.
  async function terminate(force = false) {
    setBusy("terminating");
    setError("");
    try {
      await api.terminate(instance.id, force);
      onChanged();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409 && err.body?.blocked) {
        setBlockedFiles(err.body.unpersisted_files as UnpersistedFile[]);
        setBlockedRescue((err.body.rescue as RescueReport) ?? null);
        setConfirming(false);
      } else {
        setError(err instanceof ApiError ? err.message : String(err));
        setConfirming(false);
      }
    } finally {
      setBusy("");
    }
  }

  async function toggleKeepAlive() {
    setError("");
    try {
      await api.setKeepAlive(instance.id, !instance.idle?.keep_alive);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  // Run the data-safety policy again without terminating. Worth a click when
  // the first attempt failed for a transient reason (an SSH blip), or after
  // widening the policy in Settings.
  async function retryRescue() {
    setBusy("rescuing");
    setError("");
    try {
      const { rescue } = await api.rescue(instance.id);
      if (rescue.unsaved.length === 0) {
        setNotice(
          rescue.synced_to
            ? `Saved to ${rescue.synced_to}`
            : `Saved ${rescue.downloaded.length} file(s) to ${rescue.local_dir}`,
        );
        setBlockedFiles(null);
        setBlockedRescue(null);
        await terminate(false);
        return;
      }
      setBlockedFiles(rescue.unsaved);
      setBlockedRescue(rescue);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="rounded-lg border border-zinc-200 bg-white p-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            {renaming ? (
              <form
                onSubmit={async (e) => {
                  e.preventDefault();
                  try {
                    await api.renameInstance(instance.id, newName.trim());
                    setRenaming(false);
                    onChanged();
                  } catch (err) {
                    setError(
                      err instanceof ApiError ? err.message : String(err),
                    );
                  }
                }}
                className="flex items-center gap-1.5"
              >
                <input
                  autoFocus
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  maxLength={64}
                  placeholder={instance.name || instance.id}
                  className="rounded border border-zinc-300 bg-white px-2 py-0.5 text-sm font-medium"
                />
                <button
                  type="submit"
                  className="rounded bg-zinc-900 px-2 py-0.5 text-xs font-medium text-white hover:bg-zinc-700"
                >
                  Save
                </button>
                <button
                  type="button"
                  onClick={() => setRenaming(false)}
                  className="text-xs text-zinc-500 hover:text-zinc-800"
                >
                  Cancel
                </button>
              </form>
            ) : (
              <>
                <div className="flex items-center gap-2">
                  <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium text-zinc-600 border border-zinc-200 tracking-wider">
                    {instance.provider === "gcp" ? "Google Cloud" : "Lambda"}
                  </span>
                  <h3 className="font-medium">{instance.name || instance.id}</h3>
                </div>
                <button
                  onClick={() => {
                    setNewName(instance.name || "");
                    setRenaming(true);
                  }}
                  title="Rename this instance (display name; empty restores Lambda's)"
                  className="text-xs text-zinc-400 hover:text-zinc-700"
                >
                  rename
                </button>
              </>
            )}
            <StatusBadge status={instance.status} />
          </div>
          <p className="mt-1 text-sm text-zinc-500">
            {instance.gpu_description || instance.instance_type} in{" "}
            {instance.region} at {formatMoney(instance.hourly_rate_usd)}/hr
          </p>
        </div>
        <div
          ref={actionsRef}
          className="flex items-center gap-2 overflow-hidden text-right"
        >
          {everConnected &&
            (() => {
              const dockActions = [
                ["Terminal", () => dockInstance(instance.id, instance.name || instance.id)],
                ["Chat", () => dockPanel("chat", instance.id, instance.name || instance.id)],
                ["Files", () => dockPanel("files", instance.id, instance.name || instance.id)],
                ["Browse", () => dockPanel("browse", instance.id, instance.name || instance.id)],
              ] as [string, () => void][];
              if (!collapsed) {
                return dockActions.map(([label, action]) => (
                  <button
                    key={label}
                    onClick={action}
                    title={`Open ${label.toLowerCase()} in the dock (snap it bottom or right)`}
                    className="rounded border border-zinc-300 px-3 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50"
                  >
                    {label}
                  </button>
                ));
              }
              return (
                <div className="relative">
                  <button
                    onClick={() => setMenuOpen((o) => !o)}
                    aria-label="More actions"
                    aria-expanded={menuOpen}
                    title="More panels (not enough room to show every button)"
                    className="rounded border border-zinc-300 px-2 py-1 font-mono text-xs font-medium text-zinc-700 hover:bg-zinc-50"
                  >
                    {">>"}
                  </button>
                  {menuOpen && (
                    <div className="absolute right-0 z-30 mt-1 w-32 rounded border border-zinc-200 bg-white py-1 shadow-lg">
                      {dockActions.map(([label, action]) => (
                        <button
                          key={label}
                          onClick={() => {
                            setMenuOpen(false);
                            action();
                          }}
                          className="block w-full px-3 py-1.5 text-left text-xs text-zinc-700 hover:bg-zinc-50"
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              );
            })()}
          {confirming ? (
            <div className="flex items-center gap-2">
              <span className="text-xs text-zinc-500">Terminate?</span>
              <button
                onClick={() => terminate(false)}
                disabled={busy !== ""}
                title="Saves the instance's scratch files first (Settings decides where), then stops the billing"
                className="rounded bg-red-600 px-3 py-1 text-xs font-medium text-white hover:bg-red-500 disabled:opacity-50"
              >
                {busy === "terminating"
                  ? "Saving your files..."
                  : "Save files & terminate"}
              </button>
              <button
                onClick={() => setConfirming(false)}
                disabled={busy !== ""}
                className="rounded border border-zinc-300 px-3 py-1 text-xs hover:bg-zinc-50"
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              onClick={() => setConfirming(true)}
              className="rounded border border-red-200 px-3 py-1 text-xs font-medium text-red-700 hover:bg-red-50"
            >
              Terminate
            </button>
          )}
        </div>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 text-sm md:grid-cols-4">
        <div>
          <dt className="text-xs text-zinc-400">SSH connection</dt>
          <dd className="mt-0.5">
            <StatusBadge status={instance.connection_state} />
          </dd>
        </div>
        <div>
          <dt className="text-xs text-zinc-400">Mode</dt>
          <dd className="mt-0.5 text-zinc-700">
            {instance.connection_mode ?? "unknown"}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-zinc-400">IP</dt>
          <dd className="mt-0.5 font-mono text-xs text-zinc-700">
            {instance.ip ?? "assigning..."}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-zinc-400">Filesystem</dt>
          <dd className="mt-0.5 text-zinc-700">
            {instance.filesystems.join(", ") || "none"}
          </dd>
        </div>
      </dl>

      {instance.idle && instance.connection_state === "connected" && (
        <div className="mt-2 flex items-center gap-2 text-xs">
          {instance.idle.keep_alive ? (
            <span className="text-emerald-700">
              Idle auto-termination is off; this instance runs until you
              terminate it.
            </span>
          ) : (
            <span
              className={
                instance.idle.timeout_seconds - instance.idle.idle_seconds <
                300
                  ? "font-medium text-amber-700"
                  : "text-zinc-500"
              }
            >
              Idle {Math.floor(instance.idle.idle_seconds / 60)}m; auto
              terminates after{" "}
              {Math.round(instance.idle.timeout_seconds / 60)}m idle (
              {Math.max(
                0,
                Math.ceil(
                  (instance.idle.timeout_seconds -
                    instance.idle.idle_seconds) /
                    60,
                ),
              )}
              m left)
            </span>
          )}
          {editingTimeout ? (
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                try {
                  await api.setIdleTimeout(instance.id, newTimeout ? parseInt(newTimeout, 10) : null);
                  setEditingTimeout(false);
                  onChanged();
                } catch (err) {
                  setError(err instanceof ApiError ? err.message : String(err));
                }
              }}
              className="flex items-center gap-1.5"
            >
              <select
                autoFocus
                value={newTimeout}
                onChange={(e) => setNewTimeout(e.target.value)}
                className="rounded border border-zinc-300 bg-white px-2 py-0.5 text-xs"
              >
                <option value="">Default</option>
                <option value="1800">30 min</option>
                <option value="3600">1 hour</option>
                <option value="7200">2 hours</option>
                <option value="14400">4 hours</option>
                <option value="28800">8 hours</option>
              </select>
              <button type="submit" className="rounded bg-zinc-900 px-2 py-0.5 text-xs text-white">Save</button>
              <button type="button" onClick={() => setEditingTimeout(false)} className="text-zinc-500">Cancel</button>
            </form>
          ) : (
            <button
              onClick={() => {
                setNewTimeout(instance.idle!.timeout_seconds.toString());
                setEditingTimeout(true);
              }}
              className="text-zinc-400 hover:text-zinc-700"
              title="Edit idle timeout"
            >
              (edit)
            </button>
          )}
          <button
            onClick={toggleKeepAlive}
            className="rounded border border-zinc-300 px-2 py-0.5 text-xs text-zinc-700 hover:bg-zinc-50"
          >
            {instance.idle.keep_alive ? "Resume auto-off" : "Keep alive"}
          </button>
        </div>
      )}

      {everConnected && <TelemetryChart instanceId={instance.id} />}

      {everConnected && (
        <div className="mt-3 rounded border border-zinc-200 bg-zinc-50 p-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-zinc-700">IDE & SSH</span>
            {!ideInfo ? (
              <button
                onClick={async () => {
                  setAttachingIde(true);
                  try {
                    const info = await api.attachIDE(instance.id);
                    setIdeInfo(info);
                  } catch (err) {
                    setError(err instanceof ApiError ? err.message : String(err));
                  } finally {
                    setAttachingIde(false);
                  }
                }}
                disabled={attachingIde}
                className="rounded bg-zinc-900 px-3 py-1 text-xs font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
              >
                {attachingIde ? "Configuring..." : "Configure Attach"}
              </button>
            ) : (
              <div className="flex items-center gap-2">
                <a href={ideInfo.vscode_url} className="rounded border border-zinc-300 bg-white px-3 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50">
                  Open in VS Code
                </a>
                <a href={ideInfo.cursor_url} className="rounded border border-zinc-300 bg-white px-3 py-1 text-xs font-medium text-zinc-700 hover:bg-zinc-50">
                  Open in Cursor
                </a>
              </div>
            )}
          </div>
          {ideInfo && (
            <div className="mt-2 flex items-center gap-2 rounded bg-white p-2 text-xs font-mono text-zinc-600 border border-zinc-200">
              <span className="flex-1">{ideInfo.ssh_command}</span>
              <button
                onClick={() => {
                  navigator.clipboard.writeText(ideInfo.ssh_command);
                  setCopiedCmd(true);
                  setTimeout(() => setCopiedCmd(false), 2000);
                }}
                className="rounded border border-zinc-300 px-2 py-0.5 hover:bg-zinc-50"
              >
                {copiedCmd ? "Copied!" : "Copy"}
              </button>
            </div>
          )}
        </div>
      )}

      {/* Terminal, Chat, Files, and Browse all live in the DOCK (buttons
          above): they survive page navigation there, snap bottom or right,
          and sit side by side with the local shell instead of stretching
          this card. */}

      {blockedFiles && (
        <div className="mt-3 rounded border border-amber-300 bg-amber-50 p-3">
          <p className="text-sm font-medium text-amber-900">
            Kept running: {blockedFiles.length} file
            {blockedFiles.length === 1 ? "" : "s"} could not be saved
          </p>
          <p className="mt-1 text-xs text-amber-800">
            Manifold tried to save this instance&apos;s scratch disk before
            shutting it down and could not. It is still billing, because losing
            these files is permanent and an extra billing hour is not.
          </p>

          {blockedRescue && (
            <p className="mt-2 text-xs text-amber-800">
              {blockedRescue.sync_error ? (
                <>
                  Could not copy to your Lambda filesystem:{" "}
                  <span className="font-mono">{blockedRescue.sync_error}</span>
                </>
              ) : blockedRescue.downloaded.length > 0 ? (
                <>
                  Saved {blockedRescue.downloaded.length} file(s) to{" "}
                  <span className="font-mono">{blockedRescue.local_dir}</span>.
                  These did not fit:
                </>
              ) : (
                <>
                  Nowhere to put them: turn on a destination in{" "}
                  <a href="/settings" className="underline">
                    Settings
                  </a>
                  .
                </>
              )}
            </p>
          )}

          <ul className="mt-2 max-h-40 overflow-y-auto font-mono text-xs text-amber-900">
            {blockedFiles.map((f) => (
              <li key={f.path} className="flex justify-between gap-4 py-0.5">
                <span className="truncate">{f.path}</span>
                <span className="shrink-0 text-amber-700">
                  {formatBytes(f.size_bytes)}
                </span>
              </li>
            ))}
          </ul>

          <div className="mt-3 flex flex-wrap gap-2">
            <button
              onClick={retryRescue}
              disabled={busy !== ""}
              className="rounded bg-zinc-900 px-3 py-1 text-xs font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
            >
              {busy === "rescuing"
                ? "Saving..."
                : "Try saving them again, then terminate"}
            </button>
            <button
              onClick={() => terminate(true)}
              disabled={busy !== ""}
              className="rounded border border-red-300 px-3 py-1 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"
            >
              Terminate anyway (lose {blockedFiles.length} file
              {blockedFiles.length === 1 ? "" : "s"})
            </button>
            <button
              onClick={() => setBlockedFiles(null)}
              disabled={busy !== ""}
              className="rounded border border-zinc-300 px-3 py-1 text-xs hover:bg-zinc-50"
            >
              Keep running
            </button>
          </div>
        </div>
      )}

      {notice && <p className="mt-2 text-xs text-emerald-700">{notice}</p>}
      {instance.connection_error && (
        <p className="mt-2 text-xs text-amber-700">
          Connection: {instance.connection_error}
        </p>
      )}
      {error && <p className="mt-2 text-xs text-red-700">{error}</p>}
    </div>
  );
}
