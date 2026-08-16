#!/usr/bin/env bash
# capture-freeze.sh - run this WHILE Manifold is frozen.
#
#   bash scripts/capture-freeze.sh
#
# The freeze is intermittent, happens after stepping away, and has so far
# produced only screenshots. A screenshot cannot distinguish the two
# explanations that matter:
#
#   the BACKEND is wedged   -> curl from this script is slow too
#   the WEBVIEW is wedged   -> curl is fast, and the app's own 30s client
#                              timeout is firing on a backend that answers
#                              instantly
#
# One command, run at the moment of the freeze, settles that and collects
# the rest. It only reads: no restarts, no kills, nothing terminated.
# Writes one timestamped report and prints where it went.
#
# Safe to run at any time; harmless when nothing is wrong.

set -uo pipefail

PORT="${MANIFOLD_PORT:-8000}"
BASE="http://127.0.0.1:${PORT}"
OUT="${HOME}/manifold-freeze-$(date +%Y%m%d-%H%M%S).txt"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec > >(tee "$OUT") 2>&1

echo "=== manifold freeze capture $(date) ==="
echo "backend expected at $BASE"
echo

# 1. THE decisive measurement. Endpoints in increasing order of work, each
#    with its own bound, so one hanging call cannot hide the others.
echo "--- endpoint timings (the backend-vs-webview question) ---"
for path in /health /settings/status /instances /tasks /notifications; do
  start=$(python3 -c 'import time; print(time.time())')
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 45 "${BASE}${path}" \
         -H "Authorization: Bearer ${MANIFOLD_API_TOKEN:-}" 2>/dev/null)
  end=$(python3 -c 'import time; print(time.time())')
  printf '  %-20s http %-4s %6.2fs\n' "$path" "${code:-000}" \
    "$(python3 -c "print($end-$start)")"
done
echo "  (fast here + frozen UI = the webview is the problem, not the backend)"
echo

# 2. Is anything actually burning money while this is stuck?
echo "--- instances (billing truth) ---"
# No f-strings below: a backslash inside an f-string expression is a syntax
# error before Python 3.12, and quoting one through a shell heredoc is how
# this section silently printed "(could not read)" on its first run.
curl -s --max-time 30 "${BASE}/instances" \
  -H "Authorization: Bearer ${MANIFOLD_API_TOKEN:-}" 2>/dev/null \
  | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("  (no answer - see timings above)")
    raise SystemExit
ins = d.get("instances", [])
print("  {} running{}".format(len(ins), "  [MOCK DATA]" if d.get("mock") else ""))
for i in ins:
    idle = (i.get("idle") or {}).get("idle_seconds")
    print("    {} {} {} conn={} idle={}s".format(
        i.get("name"), i.get("instance_type"), i.get("status"),
        i.get("connection_state"), idle))
'
echo

# 3. Process-level state. RSS growth over a long uptime is the leak
#    signature; a healthy backend after 24h rules that out.
echo "--- processes ---"
for pat in "uvicorn app.main" "manifold-desktop" "manifold-backend"; do
  pgrep -f "$pat" 2>/dev/null | while read -r pid; do
    ps -p "$pid" -o pid=,etime=,%cpu=,rss=,command= 2>/dev/null \
      | awk '{printf "  pid=%s up=%s cpu=%s%% rss=%.0fMB %s\n", $1,$2,$3,$4/1024,substr($0,index($0,$5),44)}'
  done
done
echo "--- webview (the app renders here) ---"
# WebContent only: Tauri's window is a WKWebView, and a looser pattern
# matched every Chromium helper on the machine (Brave's, on the first run).
ps -eo pid,rss,command 2>/dev/null | grep "[W]ebKit.WebContent" \
  | awk '{printf "  pid=%s rss=%.0fMB\n", $1,$2/1024}' | head -5
[ -z "$(pgrep -f 'WebKit.WebContent' 2>/dev/null)" ] && \
  echo "  (none - the desktop app is not running)"
echo

# 4. Descriptor and socket growth on the backend.
echo "--- backend fds / sockets ---"
# bin/uvicorn, not "uvicorn app.main": the latter also matches the `uv run`
# WRAPPER, whose 15 fds and 3 threads are not the backend's and made the
# first run of this script report a suspiciously healthy process.
BE=$(pgrep -f "bin/uvicorn app.main" | head -1)
if [ -n "${BE:-}" ]; then
  echo "  open fds: $(lsof -p "$BE" 2>/dev/null | wc -l | tr -d ' ')"
  echo "  tcp:      $(lsof -a -p "$BE" -iTCP 2>/dev/null | tail -n +2 | wc -l | tr -d ' ')"
  echo "  threads:  $(ps -M -p "$BE" 2>/dev/null | tail -n +2 | wc -l | tr -d ' ')"
else
  echo "  (no dev backend process found; the app may be serving its own)"
fi
echo

# 5. Terminal shells still alive, and what the reaper has been doing.
echo "--- recent terminal / reap audit rows ---"
curl -s --max-time 30 "${BASE}/audit?limit=60" \
  -H "Authorization: Bearer ${MANIFOLD_API_TOKEN:-}" 2>/dev/null \
  | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)["entries"]
except Exception:
    print("  (no answer)")
    raise SystemExit
hits = [e for e in rows if any(w in (e["action"] + e["detail"]).lower()
        for w in ("terminal", "shell", "reap"))]
for e in hits[:10]:
    print("  {} {:8} {:22} {}".format(
        e["at"][11:19], e["actor"], e["action"], e["detail"][:60]))
if not hits:
    print("  (none)")
'
echo

echo "--- disk pressure on the repo volume ---"
df -h "$REPO" 2>/dev/null | tail -1 | awk '{print "  "$0}'
echo
echo "=== saved to $OUT ==="
echo "Nothing was restarted, killed, or terminated by this script."
