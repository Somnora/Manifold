"use client";

import { useCallback, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { usePolling } from "@/lib/usePolling";
import { Badge } from "@/components/Badge";
import { formatBytes, formatDate } from "@/lib/format";

// Live view of what the instance is producing, plus the file bridge:
// upload local files to the box and download outputs back, all over the
// managed SSH connection (works in every region; no S3 keys needed).
export function RecentFiles({ instanceId }: { instanceId: string }) {
  const load = useCallback(
    () => api.recentFiles(instanceId),
    [instanceId],
  );
  const { data, error, refresh } = usePolling(load, 5000);
  const fileInput = useRef<HTMLInputElement>(null);
  const [dest, setDest] = useState("inbox/");
  const [uploading, setUploading] = useState(false);
  const [notice, setNotice] = useState("");
  const [uploadError, setUploadError] = useState("");

  async function onUpload(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setNotice("");
    setUploadError("");
    try {
      for (const file of Array.from(files)) {
        const result = await api.uploadFile(instanceId, file, dest);
        setNotice(
          `Uploaded ${file.name} (${formatBytes(result.bytes)}) to ${result.path}`,
        );
      }
      refresh();
    } catch (err) {
      setUploadError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  function absolutePath(f: { root: string; path: string }): string {
    return f.root === "ephemeral"
      ? `/workspace/ephemeral/${f.path}`
      : `/lambda/nfs/${f.path}`;
  }

  // Downloads mint a single-use nonce first (api.startDownload): a plain
  // href cannot carry the Authorization header, and the long-lived token
  // must never appear in a URL.
  async function download(url: string) {
    setUploadError("");
    try {
      await api.startDownload(url);
    } catch (err) {
      setUploadError(err instanceof ApiError ? err.message : String(err));
    }
  }

  if (error && !data) {
    return <p className="mt-3 text-xs text-amber-700">Files: {error}</p>;
  }
  const files = data?.files ?? [];

  return (
    <div className="mt-3 rounded border border-zinc-100 bg-zinc-50 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-zinc-500">
          Recent files (last {data?.hours ?? 24}h)
          {data?.truncated && " (large tree, list truncated)"}
        </p>
        <div className="flex items-center gap-2">
          <input
            className="w-32 rounded border border-zinc-300 bg-white px-2 py-1 font-mono text-xs"
            value={dest}
            onChange={(e) => setDest(e.target.value)}
            title="Destination on the persistent filesystem (or absolute path)"
          />
          <input
            ref={fileInput}
            type="file"
            multiple
            className="hidden"
            onChange={(e) => onUpload(e.target.files)}
          />
          <button
            onClick={() => fileInput.current?.click()}
            disabled={uploading}
            className="rounded bg-zinc-900 px-3 py-1 text-xs font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
          >
            {uploading ? "Uploading..." : "Upload files"}
          </button>
        </div>
      </div>
      {notice && <p className="mt-1 text-xs text-emerald-700">{notice}</p>}
      {uploadError && (
        <p className="mt-1 text-xs text-red-700">{uploadError}</p>
      )}

      {files.length === 0 ? (
        <p className="mt-2 text-xs text-zinc-400">Nothing written yet.</p>
      ) : (
        <ul className="mt-2 max-h-48 overflow-y-auto">
          {files.map((f) => (
            <li
              key={`${f.root}:${f.path}`}
              className="flex items-center justify-between gap-3 py-0.5 font-mono text-xs text-zinc-700"
            >
              <span className="flex min-w-0 items-center gap-2">
                <Badge
                  label={f.root}
                  tone={f.root === "persistent" ? "green" : "amber"}
                />
                <span className="truncate">{f.path}</span>
              </span>
              <span className="flex shrink-0 items-center gap-2 text-zinc-400">
                <span>
                  {formatBytes(f.size_bytes)} · {formatDate(f.modified)}
                </span>
                <button
                  onClick={() =>
                    download(api.downloadUrl(instanceId, absolutePath(f)))
                  }
                  className="rounded border border-zinc-300 bg-white px-1.5 py-0.5 text-zinc-600 hover:bg-zinc-100"
                >
                  Download
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
