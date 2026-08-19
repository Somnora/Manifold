"""cloud-init user-data generation.

The script installs Docker + NVIDIA Container Toolkit, the sidecar
(loopback-only), and the Claude Code CLI. If and only if the launch carries
a Tailscale auth key, it also installs and joins Tailscale with SSH enabled.
In both modes sshd remains the only public listener: the sidecar binds to
127.0.0.1 and Docker jobs are published to 127.0.0.1 by the dispatcher.

The sidecar source is embedded verbatim into the user-data (it is a single
file well under Lambda's 1 MB limit), so instances need no fetch-from-
somewhere step and no extra credentials.
"""

from __future__ import annotations

from pathlib import Path

from .config import RESOURCE_ROOT

SIDECAR_PATH = RESOURCE_ROOT / "sidecar" / "manifold_sidecar.py"

_TEMPLATE = """#!/bin/bash
# Manifold cloud-init: Docker + NVIDIA toolkit, sidecar (127.0.0.1 only),
# Claude Code CLI{tailscale_note}. sshd stays the only public listener.
set -euxo pipefail
exec > /var/log/manifold-init.log 2>&1

export DEBIAN_FRONTEND=noninteractive

# --- NVIDIA driver (Phase 87) ---------------------------------------------
# Lambda images ship the driver, so this whole block no-ops there - the
# `command -v` guard is the provider switch, not a template flag. Stock GCE
# Ubuntu ships NO driver, and installing one is the boot-time price of
# using a plain Ubuntu image (chosen because it consumes cloud-init
# user-data, the same rider Lambda boots with). The -gcp package carries
# prebuilt kernel modules for GCE's kernel; the fallback DKMS-builds on
# anything else. Takes several minutes: the dispatcher's GPU probe holds
# jobs until a container can actually see the card, so slow is safe.
if ! command -v nvidia-smi >/dev/null; then
  apt-get update -qq
  # Discover, never guess: prebuilt module packages exist per (driver
  # series x exact kernel build), and the series line moves. The 2026-08-15
  # gate booted a kernel with 535/565/580/595 builds available and 570 -
  # the hardcoded guess - absent; worse, the meta-package installed modules
  # for the NEXT kernel, loadable only after a reboot nobody asked for.
  # So: newest series that has modules for the RUNNING kernel, exactly.
  KVER="$(uname -r)"
  SERIES="$(apt-cache search linux-modules-nvidia- 2>/dev/null \
    | grep -oE "linux-modules-nvidia-[0-9]+-server-$KVER" \
    | sed -E "s/linux-modules-nvidia-([0-9]+)-server-.*/\\1/" \
    | sort -rn | head -1)"
  if [ -n "$SERIES" ]; then
    apt-get install -y -qq "linux-modules-nvidia-$SERIES-server-$KVER" \
      "nvidia-driver-$SERIES-server"
  else
    # No prebuilt modules for this kernel at all: DKMS builds its own.
    apt-get install -y -qq nvidia-driver-550-server
  fi
  modprobe nvidia || true
fi

# --- Docker + NVIDIA Container Toolkit (Lambda images ship the driver) ----
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
# The SSH user runs docker; ensure it can reach the daemon socket.
usermod -aG docker ubuntu || true
if ! command -v nvidia-ctk >/dev/null; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \\
    | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \\
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \\
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -qq
  apt-get install -y -qq nvidia-container-toolkit
fi
# Register the NVIDIA runtime with docker on EVERY boot, NOT only when the
# toolkit was just installed. Lambda images ship the toolkit, so the guard
# above is skipped and a freshly installed docker is never wired to the
# runtime — making `docker run --gpus all` fail with an OCI error (exit 126)
# on every GPU job. This was masked until job exit codes became honest.
# Idempotent, so safe to run unconditionally.
nvidia-ctk runtime configure --runtime=docker || echo "manifold: WARNING nvidia-ctk configure failed"
systemctl restart docker || true
# Group membership only reaches NEW login sessions, and Manifold's managed
# SSH connection is usually established while this script is still running
# - so on images where ubuntu was NOT already in the docker group (GCE;
# Lambda bakes it in), that live session got "permission denied" on the
# docker socket forever. An ACL is checked at open() time, not session
# start, so it fixes the already-open session too. Placed after the docker
# restart because restarting the daemon recreates the socket.
#
# setfacl lives in the `acl` package, which stock GCE Ubuntu does NOT ship.
# Without this install the line below was "command not found", swallowed by
# its own `|| true`, on the ONE image the fix was written for - so the fix
# shipped and never ran. Both lines say so out loud now: a repair that can
# fail silently is a repair nobody can trust.
command -v setfacl >/dev/null || apt-get install -y -qq acl || echo "manifold: WARNING acl install failed (docker socket ACL cannot be applied)"
setfacl -m u:ubuntu:rw /var/run/docker.sock || echo "manifold: WARNING setfacl on the docker socket failed (sessions opened before this may not reach docker)"
# Boot-time proof that GPU containers work, recorded in this init log (view
# it from the in-app Terminal: cat /var/log/manifold-init.log). Also warms
# the base image gpu-smoke uses.
if docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi -L; then
  echo "manifold: docker --gpus all OK"
else
  echo "manifold: WARNING docker --gpus all FAILED (GPU jobs will fail)"
fi

# --- Workspace layout ------------------------------------------------------
mkdir -p /workspace/ephemeral
chown ubuntu:ubuntu /workspace/ephemeral

# --- Sidecar: single file, loopback only, systemd-supervised ---------------
# Install deps into the SAME interpreter systemd runs (/usr/bin/python3).
# On Lambda ML images `python3` in root's PATH is often conda's, so a bare
# `python3 -m pip install` lands the packages where /usr/bin/python3 cannot
# import them and the service crash-loops on every boot ("sidecar not
# reachable"). --break-system-packages covers PEP 668 (Ubuntu 23.04+); the
# bare-retry covers older pip that rejects the flag. Non-fatal: a telemetry
# sidecar that fails to install must not brick an otherwise usable GPU box.
apt-get install -y -qq python3-pip || /usr/bin/python3 -m ensurepip || true
/usr/bin/python3 -m pip install --quiet --break-system-packages 'fastapi>=0.115' 'uvicorn>=0.30' pynvml \
  || /usr/bin/python3 -m pip install --quiet 'fastapi>=0.115' 'uvicorn>=0.30' pynvml \
  || echo "WARNING: sidecar deps failed to install; telemetry/files disabled"
install -d /opt/manifold
cat > /opt/manifold/manifold_sidecar.py <<'MANIFOLD_SIDECAR_EOF'
{sidecar_source}
MANIFOLD_SIDECAR_EOF

cat > /etc/systemd/system/manifold-sidecar.service <<'UNIT'
[Unit]
Description=Manifold GPU sidecar (loopback only)
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/manifold/manifold_sidecar.py
Restart=always
RestartSec=2
User=ubuntu

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now manifold-sidecar

# --- Claude Code CLI (auth is manual/interactive on first use) -------------
curl -fsSL https://claude.ai/install.sh | HOME=/home/ubuntu bash || true
chown -R ubuntu:ubuntu /home/ubuntu/.local || true
# The installer drops the binary in ~/.local/bin but does not put it on PATH,
# so a fresh Open Terminal shell can't find `claude`. Add it for every login
# shell (profile.d) AND interactive bash (.bashrc), so it just works.
echo 'export PATH="$HOME/.local/bin:$PATH"' > /etc/profile.d/manifold-path.sh
chmod 644 /etc/profile.d/manifold-path.sh
grep -q '.local/bin' /home/ubuntu/.bashrc 2>/dev/null \
  || echo 'export PATH="$HOME/.local/bin:$PATH"' >> /home/ubuntu/.bashrc
chown ubuntu:ubuntu /home/ubuntu/.bashrc || true
{tailscale_block}
# --- The init signal -------------------------------------------------------
# The one thing the backend reads to learn that this machine is PROVISIONED,
# not merely reachable over SSH (Phase 110). Under `set -e` reaching this
# line means every line above it succeeded, which is a stronger claim than
# cloud-init's own boot-finished can make: a reboot rewrites boot-finished at
# the end of the NEXT boot while the per-instance user-data script stays
# incomplete.
#
# /var/lib, not /var/run: /var/run is tmpfs, so a driver-upgrade reboot
# mid-setup would erase the marker while cloud-init's per-instance stage
# never runs again - and the backend would wait for a file that can no
# longer appear.
install -d /var/lib/manifold
touch /var/lib/manifold/init-done
"""

_TAILSCALE_BLOCK = """
# --- Tailscale (this launch requested tailscale mode) -----------------------
# Joins the tailnet so the orchestrator AND other machines on the tailnet
# can SSH in. Adds no public listener; tailscaled talks outbound only.
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --authkey='{authkey}' --ssh --hostname='{hostname}'
"""


def build_user_data(*, tailscale_authkey: str = "", hostname: str = "") -> str:
    """Render the cloud-init script; embeds the sidecar source."""
    sidecar_source = SIDECAR_PATH.read_text()
    if "MANIFOLD_SIDECAR_EOF" in sidecar_source:
        raise ValueError("sidecar source must not contain the heredoc marker")
    if tailscale_authkey:
        ts_block = _TAILSCALE_BLOCK.format(
            authkey=tailscale_authkey, hostname=hostname or "manifold-gpu"
        )
        note = ", Tailscale"
    else:
        ts_block = ""
        note = ""
    return _TEMPLATE.format(
        sidecar_source=sidecar_source,
        tailscale_block=ts_block,
        tailscale_note=note,
    )
