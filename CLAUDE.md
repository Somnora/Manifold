# Manifold — project conventions

Local orchestrator + dashboard for Lambda Cloud GPUs. FastAPI backend is the
single guarded gateway; dashboard and MCP server are thin clients of it.

## Commands

```bash
# Backend (from backend/)
uv sync                                                    # install deps
uv run pytest -q                                           # full test suite (mocks only, no spend)
uv run uvicorn app.main:create_default_app --factory --reload            # real mode (needs .env)
# NOTE: --reload restarts on every backend file save. Each restart re-adopts
# running instances and writes ONE reconnect_on_startup audit row — that is
# expected in dev, not a bug (see DECISIONS.md). Drop --reload for a quiet log.
MANIFOLD_MOCK=1 uv run uvicorn app.main:create_default_app --factory     # mock mode, zero spend
```

Backend runs on :8000; dashboard (from `dashboard/`): `npm run dev` on :3000,
`npm run build` to typecheck/compile.

```bash
# Full mock demo (no credentials, no spend): from backend/ and dashboard/
MANIFOLD_MOCK=1 MANIFOLD_MOCK_CAPACITY_FAILURES=2 uv run uvicorn app.main:create_default_app --factory
npm run dev     # then open http://localhost:3000
```

## Layout

- `backend/app/` — FastAPI backend
  - `config.py` — .env (secrets) + config.yaml (tunables); never mix the two
  - `lambda_api.py` — `LambdaClient` interface + real (httpx) and mock clients
  - `connections.py` — `ConnectionManager` (mode swap point) + `ManagedConnection` (SSH supervisor)
  - `storage.py` — `StorageClient` interface + S3-adapter and mock backends
  - `orchestrator.py` — launch pipeline: validate → guards → retry → persist → connect; termination safety hook; sync
  - `cloud_init.py` — user-data generation (Docker, sidecar, Claude CLI, optional Tailscale)
  - `sidecar_client.py` — `SidecarClient` interface: real (SSH port forward + httpx) and mock
  - `model_client.py` — `ModelClient` interface: chat with a model served on the instance (vllm-serve) over the same forward pattern
  - `templates.py` — job-template registry; mount rules enforced at load
  - `task_queue.py` — `TaskQueue` interface + SQLite implementation
  - `dispatcher.py` — per-instance parallel task dispatch (server+batch coexist; jobs can target an instance), idle auto-termination, capacity watches, GPU telemetry sampling, auto-manage lifecycle (queue-then-launch through the guarded paths)
  - `estimates.py` — pure cost/utilization functions: pre-launch estimate + post-run right-size hint (advisory only, off the launch path)
  - `distill.py` — pure prompt-building + allowlist validation for brain-written axolotl configs (POST /distill/config); returns for review, never saves, never trains
  - `agent.py` — Autopilot: agent loop driven by any brain; fixed action allowlist; per-action human approval gates on spend actions
  - `brains.py` — brain registry: instance-served models, local Ollama/LM Studio (auto-detected), frontier APIs (key-gated)
  - `preferences.py` — the Settings-page policies (approval gates, notification toggles, data safety). config.yaml holds the DEFAULTS; the user's choices live in SQLite and override them. Never secrets.
  - `policy.py` — the launch policy (policy.yaml): reviewable allowlists/caps per role, enforced in the orchestrator, binding everyone incl. the owner. Missing = permissive; invalid = refuses to boot.
  - `notifications.py` — `NotificationCenter`: in-app bell rows + an OS ping (macOS/Linux). Sender is injected, so tests and mock mode stay silent.
  - `doctor.py` — `manifold-backend --doctor` / `uv run manifold-doctor`: outside-in wiring checklist (backend up, token accepted, registered in which agent configs at what scope); exits nonzero when an agent would be blocked
  - `breadcrumb.py` — writes `~/.config/manifold/manifold.json` on boot (best-effort, no secrets) so agents discover the running product by probing the filesystem
  - `data_safety.py` — pure rescue decisions: what is in scope, what fits the transfer budget, path confinement. No I/O; the transport lives in the orchestrator.
  - `db.py` — SQLite schema and queries
  - `main.py` — app factory + routes only; no business logic in routes
  - `mcp_server.py` — MCP stdio bridge; HTTP-only thin client (AST-enforced), run via `uv run manifold-mcp`
- `backend/desktop.py` — desktop entrypoint (PyInstaller freezes this; serves the exported dashboard at `/`)
- `desktop/` — Tauri v2 shell (.dmg/.msi): spawns the frozen backend as a sidecar, navigates a native window to it (see docs/desktop-build.md)
- `backend/tests/` — pytest; everything runs against mocks
- `sidecar/manifold_sidecar.py` — runs ON the instance, 127.0.0.1 only; embedded into cloud-init (metrics, unpersisted/recent files, fs browse/usage/delete)
- `templates/*.yaml` — bundled job templates (vllm-serve, sglang-serve, whisper-batch, axolotl-finetune, lora-merge, tao-train, sdxl-generate, script-run, gpu-smoke; the distill chain: llm-synthesize, llm-judge, llm-eval; the Foundry trio: lerobot-act, smolvla-finetune, nanogpt-pretrain); user-authored templates live in `custom-templates/` under the data dir, same loader and mount jail, editable from the Jobs page or via MCP `save_template`
- `docs/` — user-facing guides (agent-on-gpu.md, mcp-setup.md, openai-proxy.md, data-pipeline.md, distill-your-own-model.md, train-your-own-model.md, custom-templates.md, desktop-build.md, local-hub.md, team-mode.md)
- `config.yaml` — guardrails, retry policy, SSH settings, telemetry sample interval
- `.env` — secrets only (gitignored; template in `.env.example`)
- `DECISIONS.md` — every non-obvious choice gets an entry (what/alternatives/why)

## Hard rules

- No live spend in development: tests use `MockLambdaClient` exclusively.
  Real-instance testing happens manually at phase gates.
- All guards (budget, concurrency, region match, safety hooks) live in the
  backend/orchestrator. Clients may never contain business logic or a path
  around a guard.
- Termination saves before it destroys. `orchestrator.terminate(force=False)`
  rescues the instance's ephemeral files per the data-safety policy and
  refuses only if a file could NOT be saved. No caller reimplements that
  dance; `force=true` is the single explicit "burn it".
- Nothing on a GPU instance listens on a non-loopback interface except sshd.
  All instance communication rides the managed SSH connection.
- Secrets stay in .env; never hardcode, log, or echo them.
- Connection modes (direct-ssh/tailscale) differ ONLY in the dial target.
  No endpoint, business logic, or UI may branch on mode beyond displaying it.

## Working style

- Phased delivery with hard gates: run verification, show results, wait for
  approval before the next phase. Commit at the end of each phase.
- Work on feature branches (`phase-N-...`), merge to `main` at approved gates.
- Prefer boring, readable code; the owner is learning from this codebase.
- Update DECISIONS.md whenever making a non-obvious choice.
