#!/usr/bin/env bash
# Build the desktop backend: static-export the dashboard, then freeze
# backend + assets into a PyInstaller ONEDIR tree (backend/dist/
# manifold-backend/). Onedir, not onefile: onefile re-extracts 87MB per
# spawn and macOS re-assesses every fresh extraction, which put
# multi-second tails (three 30s-deadline misses in 20 spawns under load)
# on the MCP handshake and leaked _MEI dirs on every non-graceful exit.
# Onedir loads in place: no extraction, no re-assessment, nothing to leak.
# Numbers in DECISIONS.md (phase 106); packaging in scripts/stage-sidecar.sh.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> dashboard static export"
cd "$repo/dashboard"
npm ci --no-fund --no-audit
npm run build            # next.config.ts has output: "export" -> out/

echo "==> pyinstaller freeze"
cd "$repo/backend"
uv sync --dev
SEP=":"; [ "${OS:-}" = "Windows_NT" ] && SEP=";"
uv run pyinstaller --noconfirm --clean --onedir --name manifold-backend \
  --add-data "../dashboard/out${SEP}ui" \
  --add-data "../templates${SEP}templates" \
  --add-data "../sidecar${SEP}sidecar" \
  --add-data "../config.yaml${SEP}." \
  --add-data "../docs/manifold-skill.md${SEP}docs" \
  desktop.py
echo "==> built backend/dist/manifold-backend/ (onedir)"
