#!/usr/bin/env bash
# Stage the PyInstaller ONEDIR backend for Tauri. Run after
# build-backend.sh, from anywhere; expects backend/dist/manifold-backend/.
#
# The two platforms need different layouts, because Tauri puts external
# binaries and resources in different places on each:
#
#   macOS   externalBin -> Contents/MacOS/,  resources -> Contents/Resources/
#           The onedir bootloader requires _internal NEXT TO the binary, so
#           the whole dist dir ships as a resource (Resources/backend/) and
#           a small C shim stands at the registered sidecar path and execs
#           the real binary (scripts/backend-shim.c).
#
#   Windows externalBin and resources BOTH land in the install root, so the
#           real exe is the sidecar and only _internal ships as a resource:
#           adjacency for free, no shim.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
dist="$repo/backend/dist/manifold-backend"
tauri="$repo/desktop/src-tauri"

[ -d "$dist" ] || { echo "missing $dist - run scripts/build-backend.sh first" >&2; exit 1; }

triple="$(rustc -vV | sed -n 's/^host: //p')"
mkdir -p "$tauri/binaries"
rm -rf "$tauri/backend-dist"

if [ "${OS:-}" = "Windows_NT" ]; then
    [ -f "$dist/manifold-backend.exe" ] || { echo "missing $dist/manifold-backend.exe" >&2; exit 1; }
    cp "$dist/manifold-backend.exe" "$tauri/binaries/manifold-backend-$triple.exe"
    mkdir -p "$tauri/backend-dist"
    cp -R "$dist/_internal" "$tauri/backend-dist/_internal"
else
    cc -O2 -o "$tauri/binaries/manifold-backend-$triple" "$repo/scripts/backend-shim.c"
    cp -R "$dist" "$tauri/backend-dist"
fi
echo "==> staged sidecar for $triple"
