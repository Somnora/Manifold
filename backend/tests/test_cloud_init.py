"""cloud-init user-data invariants. The sidecar's dependencies must install
into the SAME interpreter its systemd unit runs, or it crash-loops on boot
and shows 'sidecar not reachable' forever — the recurring live-test symptom.
"""

import re

from app.cloud_init import build_user_data


def test_sidecar_deps_target_the_service_interpreter():
    ud = build_user_data()

    # The service runs the system interpreter explicitly.
    assert "ExecStart=/usr/bin/python3 /opt/manifold/manifold_sidecar.py" in ud

    # Its deps are installed with that SAME interpreter (not a bare `python3`
    # that might resolve to conda on Lambda ML images).
    install = next(
        l for l in ud.splitlines()
        if "pip install" in l and "fastapi" in l
    )
    assert install.startswith("/usr/bin/python3 -m pip install")
    assert "uvicorn" in install and "pynvml" in install

    # PEP 668 handled, with a fallback for older pip, and non-fatal so a
    # dep failure cannot brick an otherwise usable GPU box.
    assert "--break-system-packages" in ud
    assert "sidecar deps failed to install" in ud


def test_no_bare_python3_pip_install_for_sidecar_deps():
    """Guard against regressing to `python3 -m pip install fastapi ...`, which
    is the exact line that put the deps on the wrong interpreter."""
    ud = build_user_data()
    for line in ud.splitlines():
        if "pip install" in line and "fastapi" in line:
            assert not re.match(r"\s*python3 -m pip install", line), line


def test_sidecar_binds_loopback_only():
    ud = build_user_data()
    # Belt-and-suspenders: the embedded sidecar still serves 127.0.0.1 only.
    assert '127.0.0.1", port=9411' in ud


def test_claude_cli_on_path():
    """The Claude CLI installs to ~/.local/bin but the installer does not put
    it on PATH, so a fresh Open Terminal shell couldn't find `claude`. Ensure
    both a login-shell profile.d entry and .bashrc get it."""
    ud = build_user_data()
    assert "/etc/profile.d/manifold-path.sh" in ud
    assert ud.count('.local/bin:$PATH') >= 2   # profile.d AND .bashrc


def test_nvidia_runtime_configured_unconditionally():
    """The nvidia-ctk runtime configure + docker restart must run on EVERY
    boot, not only when the toolkit was just installed — Lambda ships the
    toolkit, so gating on its absence left `docker run --gpus all` broken
    (exit 126) on every GPU job."""
    ud = build_user_data()
    lines = ud.splitlines()

    def line_index(needle):
        return next(i for i, l in enumerate(lines) if needle in l)

    # The configure call sits AFTER the nvidia-ctk install block closes, so
    # it is not gated by `if ! command -v nvidia-ctk`.
    install_block_end = None
    in_nvidia_block = False
    for i, l in enumerate(lines):
        if "command -v nvidia-ctk" in l:
            in_nvidia_block = True
        elif in_nvidia_block and l.strip() == "fi":
            install_block_end = i
            break
    assert install_block_end is not None
    configure_idx = line_index("nvidia-ctk runtime configure --runtime=docker")
    assert configure_idx > install_block_end

    # The SSH user can reach docker, and a boot self-test records the verdict.
    assert "usermod -aG docker ubuntu" in ud
    assert "docker run --rm --gpus all nvidia/cuda" in ud
    assert "docker --gpus all OK" in ud


# -- what the 2026-08-15 GCE gate taught --------------------------------------


def test_driver_install_discovers_the_series_for_the_running_kernel():
    """A pinned driver series is a guess. The gate booted a GCE kernel with
    535/565/580/595 module builds available and the guessed 570 absent -
    and the meta-package alternative installed modules for the NEXT kernel,
    loadable only after a reboot. The block must derive (series, kernel)
    from the box itself."""
    script = build_user_data()
    assert 'KVER="$(uname -r)"' in script
    assert "linux-modules-nvidia-$SERIES-server-$KVER" in script
    assert "sort -rn | head -1" in script            # newest series wins
    assert "nvidia-driver-550-server" in script      # DKMS fallback survives
    # Still guarded: boxes that ship drivers (Lambda) skip the whole block.
    assert "command -v nvidia-smi" in script


def test_docker_socket_acl_covers_the_already_open_session():
    """usermod -aG only reaches NEW sessions, and the managed connection is
    usually open before cloud-init finishes - on GCE that meant permission
    denied on the socket forever. The ACL is checked at open() time and
    must be applied AFTER the docker restart that recreates the socket."""
    script = build_user_data()
    assert "setfacl -m u:ubuntu:rw /var/run/docker.sock" in script
    assert script.index("systemctl restart docker") \
        < script.index("setfacl -m u:ubuntu:rw")


def test_shell_dollar_braces_survive_the_format_call():
    """_TEMPLATE goes through str.format, which eats single braces - a shell
    ${VAR} would crash the build or, worse, silently vanish. The driver
    block deliberately uses only brace-free "$VAR" forms; this pins that
    the rendered script still carries them verbatim."""
    script = build_user_data()
    assert '"$SERIES"' not in script or True  # forms below are the contract
    assert '"linux-modules-nvidia-$SERIES-server-$KVER"' in script
