# Desktop builds: .dmg and .msi

Manifold ships as a native desktop app: a Tauri window around the whole
product compiled into ONE process. Users double-click Manifold.app; nothing
else to install.

```
Manifold.app / Manifold.msi
  [ Tauri shell (~10MB) - native window, spawns and reaps the backend ]
        |
  [ manifold-backend (PyInstaller, ~40MB) - the entire product:
      FastAPI backend + templates/ + sidecar + the dashboard baked in,
      serving http://127.0.0.1:8000 on loopback only ]
```

## How the pieces map

| Dev world | Packaged world |
| --- | --- |
| `uv run uvicorn ...` on :8000 | `manifold-backend` sidecar on :8000 |
| `npm run dev` on :3000 | static export baked into the binary, served at `/` |
| repo `.env` / `config.yaml` / `manifold.db` | per-user data dir (below) |
| `templates/`, `sidecar/` in the repo | bundled read-only inside the binary |

- **RESOURCE_ROOT vs DATA_ROOT** (`backend/app/config.py`): read-only
  assets ship inside the bundle; all mutable state (secrets, tunables,
  database, host-key pins) lives in the platform data dir -
  `~/Library/Application Support/Manifold` on macOS, `%APPDATA%\Manifold`
  on Windows. First run scaffolds it and seeds `config.yaml` from the
  bundled default. Development is untouched (both roots = repo root).
- The dashboard detects where it is served from (`dashboard/lib/backend.ts`):
  same-origin when the backend serves it, `localhost:8000` when running
  under `npm run dev`. `NEXT_PUBLIC_API_URL` still overrides both.
- The shell is dumb on purpose: spawn sidecar, wait for the port, navigate,
  kill on exit. Every rule stays in the backend (thin-client rule).

## Building locally (macOS)

```bash
./scripts/build-backend.sh          # dashboard export + PyInstaller freeze
cd desktop
npm install
./scripts/prepare-sidecar.sh        # stage binary as manifold-backend-<triple>
npx tauri build --bundles dmg       # -> src-tauri/target/release/bundle/dmg/
```

Requirements: Node 20+, uv, Rust (`brew install rustup && rustup default
stable`). The first Tauri build compiles the Rust world (several minutes);
afterwards it is incremental.

Quick smoke test without the shell: `backend/dist/manifold-backend` is a
complete standalone product - run it (optionally `MANIFOLD_MOCK=1`) and
open http://127.0.0.1:8000.

## CI

`.github/workflows/desktop.yml` builds the .dmg (macos-14/arm64) and .msi
(windows-2022/x64) on every `v*` tag or manual dispatch, and uploads them
as artifacts.

## Sharing a build: GitHub Releases, not the git tree

Never commit a `.dmg`/`.msi` into the repo - git isn't built for binary
diffs, and every clone would carry that weight forever. Push a version tag
and CI attaches both installers to a **GitHub Release** instead: a stable
link at `https://github.com/<org>/<repo>/releases/latest` that anyone can
open and download from, no GitHub login required (unlike the 90-day
build-artifact uploads above, which do require one).

```bash
git tag v0.1.0
git push origin v0.1.0
```

That's it - the `release` job in the workflow waits for both platform
builds, then publishes them together as one release. Share the `/releases/latest` URL or the specific
`/releases/tag/v0.1.0` link.

> `/releases/latest` resolves to the newest NON-prerelease release, and
> 404s when every release is marked prerelease. If you publish a
> prerelease, share the `/releases/tag/<tag>` link instead - otherwise you
> are handing people a dead URL.

Since the repo is public and the bundles are unsigned (below), anyone
downloading will see a Gatekeeper/SmartScreen warning - the release notes
say so.

## Signing - the honest part

The bundles are **unsigned** until accounts exist:

- **macOS:** Gatekeeper blocks unsigned apps ("unidentified developer";
  right-click -> Open works). Fix = Apple Developer Program ($99/yr) ->
  Developer ID certificate + notarization. Tauri automates both once
  `APPLE_CERTIFICATE`/`APPLE_ID` secrets are set in the workflow.
- **Windows:** SmartScreen shows "unrecognized app" (More info -> Run
  anyway). Fix = an Authenticode certificate wired into the same step.
- An Intel-mac build is a second matrix row (macos-13) if anyone needs it.

## Known limits (v1)

- No auto-update yet; Tauri's updater plugin is the follow-up once builds
  are signed (updates must be signed to be safe).
- If :8000 is already taken by something that is not Manifold, the app
  window will show that instead; `MANIFOLD_PORT` + a rebuild changes the
  port. A dynamic-port handshake is a v2 nicety.
- The MCP server (`uv run manifold-mcp`) remains a dev-checkout feature;
  packaging it as a subcommand of the binary is a follow-up.
