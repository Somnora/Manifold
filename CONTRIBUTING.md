# Contributing to Manifold

Thanks for looking. Manifold rents real GPUs and deletes real files, so the
contribution rules are stricter than the size of the project might suggest.
Read the hard rules before you write code — a change that violates one will be
sent back no matter how good it is otherwise.

## Get it running

Everything runs against a simulated Lambda cloud. You never need credentials
and you never spend a dollar to develop or test.

```bash
# backend
cd backend
uv sync
uv run pytest -q                                                       # full suite, mocks only
MANIFOLD_MOCK=1 uv run uvicorn app.main:create_default_app --factory   # mock server on :8000

# dashboard (separate terminal)
cd dashboard
npm install
npm run dev        # :3000
npm run build      # typecheck + compile
```

Open http://localhost:3000, launch an instance, queue a `vllm-serve` job, watch
it go ready. That is the whole product, running on fixtures.

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 20+.

## The hard rules

These are architectural, not stylistic. They exist because the failure modes
are money and data loss.

1. **No live spend in development or tests.** Tests use `MockLambdaClient`
   exclusively. Real-instance testing happens manually, at phase gates, by the
   maintainer. A PR that hits the real Lambda API from a test will not merge.
2. **All guards live in the backend.** Budget caps, concurrency limits, region
   matching, and safety hooks belong in `orchestrator.py` and nowhere else. The
   dashboard, the desktop app, and the MCP server are thin clients. They may
   never contain business logic or a path around a guard. The MCP server is
   AST-enforced as HTTP-only for this reason.
3. **Termination saves before it destroys.** `orchestrator.terminate(force=False)`
   rescues the instance's ephemeral files per the data-safety policy and refuses
   if a file could *not* be saved. No caller reimplements that dance.
   `force=true` is the single explicit "burn it".
4. **Nothing on a GPU instance listens on a non-loopback interface except sshd.**
   All instance communication rides the managed SSH connection.
5. **Secrets stay in `.env`.** Never hardcode, log, or echo them. `config.yaml`
   is for tunables; the two never mix.
6. **Connection modes differ only in the dial target.** `direct-ssh` and
   `tailscale` may not cause any endpoint, business logic, or UI to branch
   beyond displaying which mode is in use.

The authoritative copy of these lives in `CLAUDE.md`.

## Making a change

- **Open an issue first** for anything beyond a typo or a small fix. Manifold is
  built in phases with hard gates, and it helps to know where your change fits
  before you spend time on it.
- **Branch from `main`**, named for what you are doing (`fix-idle-timer-race`,
  `phase-N-...` for maintainer phase work).
- **Add tests.** Every behavioral change needs coverage in `backend/tests/`.
  The suite is 970+ tests and all of it runs on mocks; match that.
- **Run the checks** before you push:
  ```bash
  cd backend && uv run pytest -q
  cd dashboard && npm run build
  ```
- **Log non-obvious choices in `DECISIONS.md`.** If you picked one approach over
  a plausible alternative, write an entry: what you chose, what else you
  considered, why. This is a real requirement, not a nicety — it is how the
  project stays understandable.
- **Keep the code boring.** Readable beats clever. The maintainer is learning
  from this codebase and so is everyone else who reads it.

## Pull requests

Describe what changed and why, note which hard rules the change touches (if
any), and confirm the test suite passes. Screenshots help for dashboard work.
Small, focused PRs get reviewed faster than large ones.

## Security

Do not open a public issue for a security problem. Email james@somnora.app
instead. Anything touching SSH supervision, cloud-init generation, credential
handling, or the safety hooks counts.

## License

By contributing you agree that your contributions are licensed under the MIT
License, the same terms that cover the rest of the project. See `LICENSE`.
