# Using Manifold as an OpenAI-compatible endpoint

Manifold exposes an OpenAI-compatible API at `http://localhost:8000/v1`.
Point any tool that speaks the OpenAI API — OpenClaw, an IDE assistant, a
script, the `openai` SDK — at that base URL, and it talks to a model
running on one of your Lambda GPUs. Your private model, your hardware, the
same API everything already supports.

## How it works

1. Launch an instance and run a `vllm-serve` job on it with the model you
   want (Jobs page, or `run_job` via MCP). vLLM serves an OpenAI-compatible
   API on the instance's loopback.
2. Point your client at `http://localhost:8000/v1`.
3. Requests route by the `model` field to the instance serving it. The
   completion streams back over the managed SSH connection.

The proxy adds **no new listener** on the instance and **launches nothing**
— it only reaches models already running (whose launch already cleared the
budget and concurrency guards). It is a router, not a spender.

## Endpoints

- `GET /v1/models` — every model currently served on a connected instance.
- `POST /v1/chat/completions` — streaming and non-streaming, all standard
  OpenAI parameters (`temperature`, `max_tokens`, `top_p`, `stop`, ...)
  passed straight through to vLLM.

## Example (the openai Python SDK)

```python
from openai import OpenAI

# api_key: MANIFOLD_PROXY_KEY if you set one, else MANIFOLD_API_TOKEN
# (both live in Manifold's .env). Any value works in mock mode.
client = OpenAI(base_url="http://localhost:8000/v1", api_key="<from .env>")

# See what's available
print([m.id for m in client.models.list().data])

# Chat
reply = client.chat.completions.create(
    model="Qwen/Qwen2.5-7B-Instruct",       # a model you're serving
    messages=[{"role": "user", "content": "Explain LoRA in one sentence."}],
)
print(reply.choices[0].message.content)

# Streaming
for chunk in client.chat.completions.create(
    model="Qwen/Qwen2.5-7B-Instruct",
    messages=[{"role": "user", "content": "Count to five."}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="")
```

Or point OpenClaw / any OpenAI-compatible tool at:

```
base_url: http://localhost:8000/v1
api_key:  (MANIFOLD_PROXY_KEY if set, else MANIFOLD_API_TOKEN from .env)
model:    <the model id from GET /v1/models>
```

## Choosing which model

The `model` field is resolved in this order:

1. An **instance id** — pins the request to that exact instance (useful when
   two instances serve the same model).
2. An exact **model id** match (e.g. `Qwen/Qwen2.5-7B-Instruct`). If several
   instances serve it, the first connected one is used.
3. If exactly **one** model is being served anywhere, any `model` value
   routes to it — so a tool with a hardcoded model name still works. The
   response reports the real served model.

If nothing is served you get a `503` with a clear message; if you name a
model that isn't served (and more than one is), a `404` listing what is.

## Authentication

The proxy accepts one of two credentials from `.env`, in this order:

1. `MANIFOLD_PROXY_KEY`, if you set one - a key you can hand to /v1
   clients without giving them the full Manifold API.
2. Otherwise `MANIFOLD_API_TOKEN`, the backend's own API token. Real mode
   generates this on first start and writes it to `.env`, so a real
   backend's proxy is protected by default.

Clients set the active credential as their `api_key` (it is sent as
`Authorization: Bearer <value>`).

**Migration note:** before the API token existed, a proxy with no
`MANIFOLD_PROXY_KEY` was open. Since the token landed, a previously-open
proxy now requires it: copy `MANIFOLD_API_TOKEN` from `.env` (the repo
root in development; the app's data folder, e.g.
`~/Library/Application Support/Manifold`, for the desktop app) into your
client's `api_key`. Shells opened from the Hub page with a model attached
get the right key injected automatically.

The proxy is only open when NEITHER value is set, which happens in mock
mode (`MANIFOLD_MOCK=1`) or if you empty `MANIFOLD_API_TOKEN` yourself.

## Notes and limits

- **Cost is the instance, not the request.** Tokens are free to the proxy;
  you pay for the GPU by the hour while the `vllm-serve` job runs. Terminate
  the instance (or stop the job) when you're done. Using the model counts as
  activity, so an instance won't be idle-terminated mid-conversation.
- **Embeddings / legacy completions** (`/v1/embeddings`, `/v1/completions`)
  aren't proxied yet — chat completions only. Add them the same way if a
  workflow needs them.
- **One turn, one instance.** The proxy doesn't load-balance across
  instances; it routes to a single serving instance per request.
