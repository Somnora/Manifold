"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { Badge } from "@/components/Badge";
import { formatBytes, formatDate } from "@/lib/format";

type Entry = { name: string; is_dir: boolean; size_bytes: number; modified: string };
type UsageChild = { name: string; is_dir: boolean; total_bytes: number; file_count: number };

// Full file browser for the instance, served by the sidecar over the
// managed SSH connection (every region, no S3 keys). Two lenses on the
// same directory: Browse (names, dirs first) and Sizes (recursive totals,
// heaviest first — the "what is eating my filesystem" cleanup view).
export function FileNavigator({ instanceId }: { instanceId: string }) {
  const [rootName, setRootName] = useState<"persistent" | "ephemeral">("persistent");
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [usage, setUsage] = useState<UsageChild[] | null>(null);
  const [usageTruncated, setUsageTruncated] = useState(false);
  const [mode, setMode] = useState<"browse" | "sizes">("browse");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<Entry | null>(null);
  const [dest, setDest] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);

  const absolute = useCallback(
    (name?: string) => {
      const rel = [path, name].filter(Boolean).join("/");
      return rootName === "persistent"
        ? `/lambda/nfs/${rel}`
        : `/workspace/ephemeral/${rel}`;
    },
    [rootName, path],
  );

  // The last path that listed successfully: when a navigation lands on a
  // missing path (stale rows clicked mid-load once stacked the folder name
  // three deep), we bounce back here instead of stranding the user.
  const lastGood = useRef("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const listing = await api.listDir(instanceId, rootName, path);
      setEntries(listing.entries);
      lastGood.current = path;
      if (mode === "sizes") {
        const u = await api.dirUsage(instanceId, rootName, path);
        setUsage(u.children);
        setUsageTruncated(u.truncated);
      } else {
        setUsage(null);
      }
    } catch (err) {
      if (
        err instanceof ApiError &&
        err.status === 404 &&
        path !== lastGood.current
      ) {
        setNotice(
          `Path not found; returned to /${lastGood.current || ""}`,
        );
        setPath(lastGood.current);
        return;
      }
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [instanceId, rootName, path, mode]);

  async function copyPath(abs: string) {
    try {
      await navigator.clipboard.writeText(abs);
      setNotice(`Copied ${abs}`);
    } catch {
      setError("Could not access the clipboard");
    }
  }

  // Downloads mint a single-use nonce first (api.startDownload): a plain
  // href cannot carry the Authorization header, and the long-lived token
  // must never appear in a URL.
  async function download(url: string) {
    setError("");
    try {
      await api.startDownload(url);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  useEffect(() => {
    load();
  }, [load]);

  const crumbs = path ? path.split("/") : [];

  async function doDelete(entry: Entry) {
    setError("");
    try {
      await api.deletePath(
        instanceId,
        rootName,
        [path, entry.name].filter(Boolean).join("/"),
        entry.is_dir,
      );
      setNotice(`Deleted ${entry.name}`);
      setConfirmDelete(null);
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setConfirmDelete(null);
    }
  }

  async function onUpload(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError("");
    try {
      for (const file of Array.from(files)) {
        await api.uploadFile(instanceId, file, absolute() + "/");
      }
      setNotice(`Uploaded ${files.length} file(s) here`);
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  const usageByName = new Map((usage ?? []).map((u) => [u.name, u]));
  const rows =
    mode === "sizes" && usage
      ? [...entries].sort(
          (a, b) =>
            (usageByName.get(b.name)?.total_bytes ?? 0) -
            (usageByName.get(a.name)?.total_bytes ?? 0),
        )
      : entries;

  return (
    <div className="mt-3 rounded border border-zinc-200 bg-white">
      {/* toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-100 px-3 py-2">
        <div className="flex items-center gap-2 text-xs">
          <select
            className="rounded border border-zinc-300 bg-white px-1.5 py-1"
            value={rootName}
            onChange={(e) => {
              setRootName(e.target.value as "persistent" | "ephemeral");
              setPath("");
            }}
          >
            <option value="persistent">persistent (/lambda/nfs)</option>
            <option value="ephemeral">ephemeral (/workspace)</option>
          </select>
          <button
            onClick={() => setPath(crumbs.slice(0, -1).join("/"))}
            disabled={!path || loading}
            className="rounded border border-zinc-300 px-2 py-0.5 text-zinc-600 hover:bg-zinc-50 disabled:opacity-40"
            title="Up one folder"
          >
            Up
          </button>
          <nav className="flex items-center gap-1 font-mono">
            <button className="text-zinc-500 hover:underline" onClick={() => setPath("")}>
              /
            </button>
            {crumbs.map((crumb, i) => (
              <span key={i} className="flex items-center gap-1">
                <button
                  className="text-zinc-700 hover:underline"
                  onClick={() => setPath(crumbs.slice(0, i + 1).join("/"))}
                >
                  {crumb}
                </button>
                {i < crumbs.length - 1 && <span className="text-zinc-600">/</span>}
              </span>
            ))}
          </nav>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex overflow-hidden rounded border border-zinc-300 text-xs">
            {(["browse", "sizes"] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`px-2 py-1 ${
                  mode === m ? "bg-zinc-900 text-white" : "text-zinc-600 hover:bg-zinc-50"
                }`}
              >
                {m === "browse" ? "Browse" : "Sizes"}
              </button>
            ))}
          </div>
          <input
            ref={fileInput}
            type="file"
            multiple
            className="hidden"
            onChange={(e) => onUpload(e.target.files)}
          />
          <button
            onClick={() => copyPath(absolute())}
            className="rounded border border-zinc-300 px-2 py-1 text-xs hover:bg-zinc-50"
            title="Copy this folder's absolute path"
          >
            Copy path
          </button>
          <button
            onClick={() => fileInput.current?.click()}
            disabled={uploading}
            className="rounded border border-zinc-300 px-2 py-1 text-xs hover:bg-zinc-50 disabled:opacity-50"
          >
            {uploading ? "Uploading..." : "Upload here"}
          </button>
          {path && (
            <button
              onClick={() => download(api.archiveUrl(instanceId, absolute()))}
              className="rounded bg-zinc-900 px-2 py-1 text-xs font-medium text-white hover:bg-zinc-700"
            >
              Download folder (.tar.gz)
            </button>
          )}
        </div>
      </div>

      {mode === "sizes" && usageTruncated && (
        <p className="border-b border-amber-100 bg-amber-50 px-3 py-1 text-xs text-amber-800">
          Very large tree; sizes below are a partial count.
        </p>
      )}
      {notice && <p className="px-3 pt-2 text-xs text-emerald-700">{notice}</p>}
      {error && <p className="px-3 pt-2 text-xs text-red-700">{error}</p>}

      {/* listing — drag the bottom edge to resize, like the terminal */}
      <div className="h-96 min-h-40 max-h-[85vh] resize-y overflow-auto">
        {loading && entries.length === 0 ? (
          <p className="p-4 text-center text-xs text-zinc-400">Loading…</p>
        ) : rows.length === 0 ? (
          <p className="p-4 text-center text-xs text-zinc-400">Empty directory.</p>
        ) : (
          <table className="w-full text-xs">
            <tbody className="divide-y divide-zinc-50">
              {rows.map((entry) => {
                const u = usageByName.get(entry.name);
                return (
                  <tr key={entry.name} className="group hover:bg-zinc-50">
                    <td className="px-3 py-1.5">
                      {entry.is_dir ? (
                        <button
                          className="font-mono font-medium text-zinc-800 hover:underline disabled:opacity-40"
                          disabled={loading}
                          onClick={() => {
                            // Ignore clicks while a listing is in flight:
                            // stale rows once let repeat-clicks stack the
                            // folder name onto the path multiple times.
                            if (loading) return;
                            setPath(
                              [path, entry.name].filter(Boolean).join("/"),
                            );
                          }}
                        >
                          {entry.name}/
                        </button>
                      ) : (
                        <span className="font-mono text-zinc-700">{entry.name}</span>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-3 py-1.5 text-right font-mono text-zinc-500">
                      {mode === "sizes" && u
                        ? `${formatBytes(u.total_bytes)}${
                            entry.is_dir ? ` · ${u.file_count} files` : ""
                          }`
                        : entry.is_dir
                          ? ""
                          : formatBytes(entry.size_bytes)}
                    </td>
                    <td className="whitespace-nowrap px-3 py-1.5 text-right text-zinc-400">
                      {formatDate(entry.modified)}
                    </td>
                    <td className="whitespace-nowrap px-3 py-1.5 text-right">
                      <span className="invisible flex justify-end gap-1 group-hover:visible">
                        <button
                          onClick={() => copyPath(absolute(entry.name))}
                          className="rounded border border-zinc-300 px-1.5 py-0.5 text-zinc-600 hover:bg-zinc-100"
                          title="Copy absolute path"
                        >
                          Copy path
                        </button>
                        {!entry.is_dir && (
                          <button
                            onClick={() =>
                              download(
                                api.downloadUrl(instanceId, absolute(entry.name)),
                              )
                            }
                            className="rounded border border-zinc-300 px-1.5 py-0.5 text-zinc-600 hover:bg-zinc-100"
                          >
                            Download
                          </button>
                        )}
                        {entry.is_dir && (
                          <button
                            onClick={() =>
                              download(
                                api.archiveUrl(instanceId, absolute(entry.name)),
                              )
                            }
                            className="rounded border border-zinc-300 px-1.5 py-0.5 text-zinc-600 hover:bg-zinc-100"
                          >
                            .tar.gz
                          </button>
                        )}
                        <button
                          onClick={() => setConfirmDelete(entry)}
                          className="rounded border border-red-200 px-1.5 py-0.5 text-red-700 hover:bg-red-50"
                        >
                          Delete
                        </button>
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* delete confirmation */}
      {confirmDelete && (
        <div className="border-t border-red-200 bg-red-50 px-3 py-2 text-xs text-red-900">
          {confirmDelete.is_dir ? (
            <span>
              Delete <span className="font-mono">{confirmDelete.name}/</span>{" "}
              and <span className="font-medium">everything inside it</span>?
              This cannot be undone.
            </span>
          ) : (
            <span>
              Delete <span className="font-mono">{confirmDelete.name}</span>?
            </span>
          )}
          <span className="ml-2 inline-flex gap-2">
            <button
              onClick={() => doDelete(confirmDelete)}
              className="rounded bg-red-600 px-2 py-0.5 font-medium text-zinc-900 hover:bg-red-500"
            >
              Delete
            </button>
            <button
              onClick={() => setConfirmDelete(null)}
              className="rounded border border-zinc-300 bg-white px-2 py-0.5 text-zinc-700 hover:bg-zinc-100"
            >
              Cancel
            </button>
          </span>
        </div>
      )}
    </div>
  );
}
