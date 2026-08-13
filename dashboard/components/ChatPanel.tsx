"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { API_BASE } from "@/lib/backend";
import { authHeaders } from "@/lib/token";
import { usePolling } from "@/lib/usePolling";


// OpenAI content parts, so vision models can receive images.
type ContentPart =
  | { type: "text"; text: string }
  | { type: "image_url"; image_url: { url: string } };

type Message =
  | { kind: "chat"; role: "user" | "assistant"; content: string; images?: string[] }
  | { kind: "tool"; action: string; ok: boolean; error?: string };

// Chat with the model served on THIS instance (a running vllm-serve job).
// Tools mode (default on): the backend runs a guarded action loop so the
// model can browse/read the instance's filesystems and queue jobs — replies
// then arrive turn-at-once. Toggle Tools off for pure token streaming.
export function ChatPanel({ instanceId }: { instanceId: string }) {
  const load = useCallback(() => api.modelStatus(instanceId), [instanceId]);
  const { data: model } = usePolling(load, 5000);

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [images, setImages] = useState<string[]>([]); // pending data-URLs
  const [tools, setTools] = useState(true);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages]);

  useEffect(() => () => abortRef.current?.abort(), []);

  function addImageFiles(files: FileList | File[]) {
    for (const f of Array.from(files)) {
      if (!f.type.startsWith("image/")) continue;
      const reader = new FileReader();
      reader.onload = () =>
        setImages((prev) => [...prev, String(reader.result)]);
      reader.readAsDataURL(f);
    }
  }

  // History for the API: tool lines are display-only; images become
  // OpenAI content parts (only vision models can actually read them).
  function apiHistory(all: Message[]) {
    return all
      .filter((m): m is Extract<Message, { kind: "chat" }> => m.kind === "chat")
      .map((m) => {
        if (m.role === "user" && m.images?.length) {
          const parts: ContentPart[] = [
            { type: "text", text: m.content },
            ...m.images.map((url) => ({
              type: "image_url" as const,
              image_url: { url },
            })),
          ];
          return { role: m.role, content: parts };
        }
        return { role: m.role, content: m.content };
      });
  }

  async function send(e: React.FormEvent) {
    e.preventDefault();
    const content = input.trim();
    if ((!content && images.length === 0) || streaming) return;
    setInput("");
    setError("");
    const sent: Message = {
      kind: "chat", role: "user", content, images: images.slice(),
    };
    setImages([]);
    const history: Message[] = [...messages, sent];
    setMessages([...history, { kind: "chat", role: "assistant", content: "" }]);
    setStreaming(true);

    const abort = new AbortController();
    abortRef.current = abort;
    try {
      // Raw fetch (request() cannot stream SSE), so the auth header rides
      // here explicitly like every other escape from request().
      const resp = await fetch(`${API_BASE}/instances/${instanceId}/chat`, {
        method: "POST",
        headers: { "content-type": "application/json", ...authHeaders() },
        body: JSON.stringify({ messages: apiHistory(history), tools }),
        signal: abort.signal,
      });
      if (!resp.ok || !resp.body) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.detail ?? `HTTP ${resp.status}`);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE events are separated by blank lines; keep the tail partial.
        const events = buffer.split("\n\n");
        buffer = events.pop() ?? "";
        for (const event of events) {
          const data = event
            .split("\n")
            .filter((l) => l.startsWith("data: "))
            .map((l) => l.slice(6))
            .join("");
          if (!data || data === "[DONE]") continue;
          try {
            const chunk = JSON.parse(data);
            if (chunk.error) throw new Error(chunk.error);
            if (chunk.tool) {
              // A guarded tool call ran on the backend: show it inline,
              // keeping the pending assistant bubble last.
              setMessages((m) => [
                ...m.slice(0, -1),
                {
                  kind: "tool",
                  action: chunk.tool.action,
                  ok: chunk.tool.ok,
                  error: chunk.tool.error ?? undefined,
                },
                m[m.length - 1],
              ]);
              continue;
            }
            const delta: string = chunk.choices?.[0]?.delta?.content ?? "";
            if (delta) {
              setMessages((m) => {
                const out = [...m];
                const last = out[out.length - 1];
                if (last.kind === "chat") {
                  out[out.length - 1] = {
                    ...last,
                    content: last.content + delta,
                  };
                }
                return out;
              });
            }
          } catch (err) {
            if (err instanceof Error && !(err instanceof SyntaxError)) {
              throw err;
            }
            // Ignore unparseable keep-alive lines.
          }
        }
      }
    } catch (err) {
      if (!(err instanceof DOMException && err.name === "AbortError")) {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }

  if (!model?.serving) {
    return (
      <div className="mt-3 rounded border border-zinc-100 bg-zinc-50 p-4 text-sm text-zinc-500">
        No model is being served here yet. Queue a{" "}
        <span className="font-mono text-xs">vllm-serve</span> job on the Jobs
        page with the HuggingFace model you want; once it is ready, the chat
        opens automatically. (First run downloads the weights to persistent
        storage — subsequent serves start much faster.)
      </div>
    );
  }

  if (!model.ready) {
    return (
      <div className="mt-3 flex items-start gap-3 rounded border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
        <span className="mt-1.5 h-2 w-2 shrink-0 animate-pulse rounded-full bg-amber-500" />
        <span>
          <span className="font-mono">{model.model_id}</span> is loading on
          this instance: downloading the weights (a 7B model is ~15 GB on
          first serve) and warming up the GPU. Chat opens automatically when
          it is ready — this can take several minutes. Watch real progress in
          the job&apos;s <span className="font-medium">Logs</span> on the Jobs
          page (look for the model shards downloading).
          {model.status_detail ? (
            <span className="mt-1 block text-xs text-amber-700">
              Not answering yet; normal while it loads (probe:{" "}
              {model.status_detail}).
            </span>
          ) : null}
        </span>
      </div>
    );
  }

  return (
    <div className="mt-3 overflow-hidden rounded border border-zinc-200">
      <div className="flex items-center justify-between border-b border-zinc-200 bg-zinc-50 px-3 py-1.5">
        <span className="text-xs text-zinc-500">
          Chatting with{" "}
          <span className="font-mono text-zinc-700">{model.model_id}</span> on
          this instance
        </span>
        <span className="flex items-center gap-3">
          <label
            className="flex cursor-pointer items-center gap-1 text-xs text-zinc-500"
            title="Let the model browse/read this instance's filesystems and queue jobs (guarded backend loop). Off = pure streaming chat."
          >
            <input
              type="checkbox"
              checked={tools}
              onChange={(e) => setTools(e.target.checked)}
            />
            Tools
          </label>
          {streaming && (
            <button
              onClick={() => abortRef.current?.abort()}
              className="rounded border border-zinc-300 px-2 py-0.5 text-xs hover:bg-zinc-100"
            >
              Stop
            </button>
          )}
        </span>
      </div>

      {/* resize-y: drag the bottom edge to stretch the conversation area. */}
      <div
        ref={scrollRef}
        className="h-96 min-h-40 resize-y space-y-3 overflow-y-auto p-3"
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          addImageFiles(e.dataTransfer.files);
        }}
      >
        {messages.length === 0 && (
          <p className="text-sm text-zinc-400">
            Say something — tokens are generated on this GPU and streamed back
            over the managed SSH connection. With Tools on, the model can list
            and read files on the instance&apos;s filesystems and queue jobs.
            Drag an image in to send it (vision models only).
          </p>
        )}
        {messages.map((m, i) =>
          m.kind === "tool" ? (
            <p key={i} className="text-xs text-zinc-400">
              <span className="font-mono">
                tool: {m.action} {m.ok ? "ok" : `failed${m.error ? `: ${m.error}` : ""}`}
              </span>
            </p>
          ) : (
            <div
              key={i}
              className={m.role === "user" ? "flex justify-end" : "flex"}
            >
              <div
                className={
                  m.role === "user"
                    ? "max-w-[80%] rounded-lg bg-zinc-900 px-3 py-2 text-sm text-white"
                    : "max-w-[80%] whitespace-pre-wrap rounded-lg bg-zinc-100 px-3 py-2 text-sm text-zinc-800"
                }
              >
                {m.images?.map((url, j) => (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    key={j}
                    src={url}
                    alt="attached"
                    className="mb-2 max-h-48 rounded"
                  />
                ))}
                {m.content ||
                  (streaming && i === messages.length - 1 ? "…" : "")}
              </div>
            </div>
          ),
        )}
      </div>

      {error && (
        <p className="border-t border-red-100 bg-red-50 px-3 py-2 text-xs text-red-700">
          {error}
        </p>
      )}

      {images.length > 0 && (
        <div className="flex items-center gap-2 border-t border-zinc-200 bg-zinc-50 px-3 py-2">
          {images.map((url, i) => (
            // eslint-disable-next-line @next/next/no-img-element
            <img key={i} src={url} alt="pending" className="h-12 rounded" />
          ))}
          <button
            type="button"
            onClick={() => setImages([])}
            className="text-xs text-zinc-500 hover:text-zinc-800"
          >
            Clear
          </button>
          <span className="text-[11px] text-zinc-400">
            Sent as image input — the served model must be a vision model
            (e.g. Qwen2.5-VL) or it will error.
          </span>
        </div>
      )}

      <form onSubmit={send} className="flex gap-2 border-t border-zinc-200 p-2">
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          multiple
          className="hidden"
          onChange={(e) => {
            if (e.target.files) addImageFiles(e.target.files);
            e.target.value = "";
          }}
        />
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          title="Attach an image (vision models only)"
          className="rounded border border-zinc-300 px-2.5 py-1.5 text-sm text-zinc-600 hover:bg-zinc-50"
        >
          Img
        </button>
        <input
          className="flex-1 rounded border border-zinc-300 px-2.5 py-1.5 text-sm"
          placeholder={`Message ${model.model_id}...`}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={streaming}
        />
        <button
          type="submit"
          disabled={streaming || (!input.trim() && images.length === 0)}
          className="rounded bg-zinc-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </div>
  );
}
