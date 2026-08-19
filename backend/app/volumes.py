"""GCP data volumes: the decisions, with no I/O.

A volume is a Compute Engine Persistent Disk that Manifold created, attached
to exactly one instance, and mounted at /lambda/nfs/<name> - the same path a
Lambda filesystem lands on, so dispatch, {persistent} substitution, the file
routes and the rescue all work unchanged. The launch row's existing
`filesystem` column carries the name: that column never meant "a Lambda
filesystem", it means "the name under /lambda/nfs/ where this launch's
persistent home is mounted", and a mounted PD satisfies that identically.

This module is the pure half, in the shape data_safety.py already uses: the
decisions live here and are pinned by tests, while the transport (SSH,
mkfs, mount) lives in the orchestrator, which owns the connections. Nothing
here touches a disk, a database or a network.

THE HAZARD THIS MODULE EXISTS TO PREVENT. Every write to /lambda/nfs/<name>
- the job wrapper's log directory, the rescue's rsync - is a plain path. If
the volume is NOT mounted there, those writes silently land on the
auto-delete boot disk: the rescue then reports everything saved, the
termination safety hook waves the destroy through, and the user's data burns
while the audit log says it was rescued. So every write is preceded by
`mountpoint -q` (see mount_guard), and a launch carrying a volume is not
promoted to active until that check passes.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

# Where a volume is mounted on the instance. The same root Lambda mounts its
# filesystems under, deliberately: every consumer of the `filesystem` column
# already builds paths from it.
MOUNT_ROOT = "/lambda/nfs"

# GCE's own disk-name charset, enforced before a name touches anything.
# These names are the first user-supplied strings this codebase interpolates
# into shell commands (the rescue's rsync destination, the dispatcher's log
# paths) and into a /dev/disk/by-id link. Lambda's names came from Lambda's
# registry; these come from Manifold's own route, so the route is where the
# charset is proven. The pattern is Google's, not a loosened version of it.
NAME_PATTERN = re.compile(r"^[a-z][-a-z0-9]{0,61}[a-z0-9]$")

# The one filesystem Manifold ever writes. Recorded per volume rather than
# assumed, so a mount can verify what it is about to mount is what we made.
DEFAULT_FSTYPE = "ext4"

# Provisioned size bounds. GCE's own floor for pd-balanced is 10 GiB; the
# ceiling is Manifold's, so an accidental extra digit is refused here rather
# than billed for a month.
MIN_SIZE_GB = 10
MAX_SIZE_GB = 4096

# Google's published on-demand list price for a pd-balanced disk in
# us-central1, recorded on the date below. A DISK BILLS FOR ITS PROVISIONED
# SIZE whether it is attached to anything or not - which is the whole reason
# GET /volumes is sourced from GCE rather than from SQLite. Labelled, never
# presented as a meter reading (the money rule: exact or labelled).
PRICE_RECORDED = "2026-08-19"
PRICE_USD_PER_GB_MONTH = 0.10
PRICE_BASIS = (
    f"Google's on-demand list price for a pd-balanced disk in us-central1 "
    f"as recorded {PRICE_RECORDED} (${PRICE_USD_PER_GB_MONTH:.2f} per "
    f"GiB-month), not a live meter. A disk bills for its provisioned size "
    f"whether an instance is attached or not."
)

# The exit code a mount guard uses to say "the destination is NOT a mount".
# Distinct from anything rsync, docker or a job is likely to return, so a
# caller can tell "we refused to write" apart from "the write failed".
NOT_A_MOUNT_EXIT = 78

# Exit codes the format/mount probe uses to report what it found. Values,
# not prose, because the caller has to branch on them.
PROBE_NO_DEVICE = 3      # /dev/disk/by-id/google-<name> is not there
PROBE_BLKID_FAILED = 4   # blkid itself errored: we did NOT learn "blank"
PROBE_DIR_NOT_EMPTY = 5  # /lambda/nfs/<name> holds files and is unmounted


def mount_path(name: str) -> str:
    """Where volume `name` is mounted on the instance."""
    return f"{MOUNT_ROOT}/{name}"


def device_link(name: str) -> str:
    """The stable device path for an attached volume.

    /dev/disk/by-id/google-<device_name>, NEVER /dev/sdb: kernel device
    letters are enumeration-ordered, so on a box with two disks the letter
    that meant the data volume last boot can mean the boot disk this one -
    and mkfs against the wrong letter is unrecoverable. The by-id link is
    built from the device_name the attach set, which is the volume's name.
    """
    return f"/dev/disk/by-id/google-{name}"


def validate_name(name: str) -> str | None:
    """Why this volume name is unusable, or None when it is fine.

    Checked before the name reaches GCE, a shell command, or a device path.
    The message states Google's rule rather than paraphrasing it, because
    the rule is Google's and a paraphrase would drift from the API error.
    """
    if not name:
        return ("A volume needs a name. GCE disk names are 1-63 characters: "
                "lowercase letters, digits and dashes, starting with a "
                "letter and ending with a letter or digit.")
    if not NAME_PATTERN.fullmatch(name):
        return (
            f"'{name}' is not a usable volume name. GCE disk names are 1-63 "
            f"characters: lowercase letters, digits and dashes, starting "
            f"with a letter and ending with a letter or digit. Manifold "
            f"mounts the volume at {mount_path('<name>')} and builds a "
            f"device path from the same string, so the name has to hold up "
            f"as a path as well as an API argument.")
    return None


def validate_size_gb(size_gb: int) -> str | None:
    """Why this size is refused, or None when it is fine."""
    if size_gb < MIN_SIZE_GB:
        return (f"{size_gb} GiB is below the {MIN_SIZE_GB} GiB minimum GCE "
                f"allows for a pd-balanced disk.")
    if size_gb > MAX_SIZE_GB:
        return (f"{size_gb} GiB is above the {MAX_SIZE_GB} GiB ceiling "
                f"Manifold will create in one call. A disk bills for its "
                f"provisioned size for as long as it exists, so an extra "
                f"digit is refused here rather than billed for a month. "
                f"Create it in the Google Cloud console if you really want "
                f"one that large.")
    return None


def mount_guard(name: str) -> str:
    """Shell that refuses, loudly, unless /lambda/nfs/<name> is a real mount.

    THE POINT OF THE WHOLE PHASE. Prefixed to every command that writes
    under that path. Without it an unmounted volume turns the path into an
    ordinary directory on the auto-delete boot disk, and every writer -
    the job wrapper, the rescue's rsync - succeeds against storage that
    dies with the instance.

    Unconditional, on Lambda too. There it converts "the NFS quietly fell
    off" from a silent fake-rescue into an honest failure, which is the
    same trade.

    Composes with `&&`: `guard && cmd` parses as `(mountpoint || {...}) &&
    cmd`, so a passing check runs cmd and a failing one exits before the
    `&&` is ever reached.
    """
    path = shlex.quote(mount_path(name))
    message = shlex.quote(
        f"manifold: {mount_path(name)} is not a mounted filesystem; "
        f"refusing to write to it (an unmounted path here is the boot disk, "
        f"which is deleted with the instance)")
    return (f"mountpoint -q {path} || "
            f"{{ echo {message} >&2; exit {NOT_A_MOUNT_EXIT}; }}")


def mounted_probe(name: str) -> str:
    """Exit 0 when /lambda/nfs/<name> is a real mount. The one question the
    gate will not promote a volume launch without a yes to."""
    return f"mountpoint -q {shlex.quote(mount_path(name))}"


def probe_command(name: str) -> str:
    """One shell round trip that answers everything the table needs.

    Asks, in ONE session so the answers cannot drift apart before the
    decision is acted on:
      - is the by-id link there at all (is the disk really attached)?
      - what does blkid say is on it?

    blkid exits 2 for "no filesystem signature", which is a real answer and
    the one that permits a format. Every OTHER nonzero exit is blkid
    failing, which is NOT the same fact - swallowing it with `|| true`
    would turn "we could not look" into "it is blank" and then mkfs a disk
    holding data. That is why the exit codes are separated here rather than
    inferred from empty output.
    """
    dev = shlex.quote(device_link(name))
    return (
        f"test -e {dev} || exit {PROBE_NO_DEVICE}; "
        f"out=$(sudo -n blkid -p -o export {dev} 2>/dev/null); rc=$?; "
        f"if [ $rc -eq 0 ] || [ $rc -eq 2 ]; then printf '%s' \"$out\"; "
        f"exit 0; fi; exit {PROBE_BLKID_FAILED}"
    )


def format_command(name: str, fstype: str) -> str:
    """mkfs on the by-id link. Issued at most once in a volume's life.

    -F is required because the target is a whole disk rather than a
    partition, and mkfs.ext4 otherwise stops to ask. It is also the flag
    that would overwrite an existing filesystem, so the ONLY thing standing
    between it and someone's data is the blkid answer read moments ago in
    the same session, plus a formatted_at that was NULL and is now written.
    Both are checked in plan_prepare; neither is checked here.
    """
    return (f"sudo -n mkfs.{fstype} -F -q -L manifold "
            f"{shlex.quote(device_link(name))}")


def mount_command(name: str, fstype: str) -> str:
    """Create the mount point, mount the volume, hand it to the SSH user.

    Refuses (PROBE_DIR_NOT_EMPTY) if /lambda/nfs/<name> already exists with
    files in it and nothing mounted there: mounting would shadow whatever
    wrote them, and those files are evidence of a problem rather than
    clutter to paper over.

    `discard,defaults` is Google's documented option set for Persistent
    Disk. The chown is the mount ROOT only, not recursive: mkfs leaves a
    root-owned filesystem, and jobs run as ubuntu, so without it the very
    first `mkdir -p .../task-logs` fails.
    """
    path = shlex.quote(mount_path(name))
    dev = shlex.quote(device_link(name))
    return (
        f"if [ -d {path} ] && [ -n \"$(ls -A {path} 2>/dev/null)\" ]; then "
        f"exit {PROBE_DIR_NOT_EMPTY}; fi; "
        f"sudo -n mkdir -p {path} && "
        f"sudo -n mount -t {fstype} -o discard,defaults {dev} {path} && "
        f"sudo -n chown ubuntu:ubuntu {path} && "
        f"mountpoint -q {path}"
    )


def fstype_of(blkid_output: str) -> str:
    """The TYPE= field of `blkid -o export` output, or "" when absent."""
    for line in blkid_output.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "TYPE":
            return value.strip()
    return ""


@dataclass(frozen=True)
class VolumePlan:
    """What to do with one attached volume, and why.

    action: "format" (then mount), "mount", or "refuse".
    terminal: a refusal that cannot become true by waiting. The gate fails
              such a launch immediately instead of billing out the whole
              provisioning budget for a state that will never change.
    """
    action: str
    fstype: str = ""
    detail: str = ""
    terminal: bool = False


def plan_prepare(*, name: str, known: bool, link_present: bool,
                 blkid_ok: bool, blkid_output: str,
                 formatted_at: str | None,
                 recorded_fstype: str | None) -> VolumePlan:
    """The format/mount decision table. Pure; the caller does the I/O.

    `known` is whether Manifold holds a volumes row for this name - its own
    private record that it created the disk and what it has done to it.
    `formatted_at` and `recorded_fstype` come from that row; `link_present`
    and `blkid_output` come from ONE probe on the box, read immediately
    before the decision is acted on.

    THE TABLE, and why each cell reads the way it does:

                     | formatted_at NULL        | formatted_at set
      ---------------+--------------------------+---------------------------
      blkid blank    | the one permitted mkfs   | LOUD REFUSAL: an
                     |                          | interrupted mkfs is
                     |                          | indistinguishable from
                     |                          | never-formatted
      ---------------+--------------------------+---------------------------
      blkid non-blank| REFUSE: a filesystem we  | verify the type matches
                     | have no record of        | what we recorded, then
                     | writing                  | mount

    Two invariants hold the whole thing up. mkfs requires BOTH a blank
    blkid and a NULL formatted_at, verified together, in the same session,
    immediately before the command. And formatted_at is written BEFORE mkfs
    is issued, never after - which is what makes a backend restart
    mid-format safe: the row says formatted, blkid comes back blank, this
    table refuses loudly, and the box is untouched.

    The formatted-but-blank cell can say something unusually strong.
    formatted_at transitions exactly once, so a volume in that state
    provably never held user data - nothing was ever mounted on it to write
    any. Saying so is what makes "delete it and create another" safe advice
    instead of a shrug.
    """
    path = mount_path(name)
    if not known:
        # A disk GCE knows about and Manifold does not: hand-created, or a
        # SQLite restored from before this volume existed. Manifold cannot
        # say whether it holds data, so it neither formats it nor mounts it
        # over. Attaching existing disks is a deliberate non-goal in v1.
        return VolumePlan(
            "refuse", terminal=True,
            detail=(
                f"Manifold has no record of volume '{name}'. It will not "
                f"format a disk it did not create, and it will not mount "
                f"one it cannot describe - either could be somebody's data. "
                f"Create a volume through Manifold, or attach this disk in "
                f"the Google Cloud console and use it outside Manifold."))
    if not link_present:
        # Usually just early: the guest agent creates the by-id symlink
        # shortly after the disk appears. Not terminal - the next tick asks
        # again, and the provisioning budget bounds the wait.
        return VolumePlan(
            "refuse",
            detail=(
                f"{device_link(name)} is not present on the instance, so "
                f"the volume is not attached (or not attached under this "
                f"name) yet."))
    if not blkid_ok:
        # "We could not look" is never "it is blank". Retryable.
        return VolumePlan(
            "refuse",
            detail=(
                f"blkid could not read {device_link(name)}, so Manifold "
                f"does not know whether the volume holds a filesystem. It "
                f"will not format or mount on a guess."))

    found = fstype_of(blkid_output)
    if not found:
        if formatted_at is None:
            return VolumePlan("format", fstype=DEFAULT_FSTYPE,
                              detail=f"{device_link(name)} holds no "
                                     f"filesystem and has never been "
                                     f"formatted by Manifold")
        return VolumePlan(
            "refuse", terminal=True,
            detail=(
                f"Volume '{name}' is recorded as formatted at "
                f"{formatted_at}, but the disk holds no filesystem. That is "
                f"an interrupted mkfs, and it is indistinguishable from a "
                f"disk that was never formatted - so Manifold will not run "
                f"mkfs again and will not mount it. Because the record is "
                f"written once, before the format, this volume provably "
                f"never held any of your data: delete it and create "
                f"another."))

    if formatted_at is None:
        return VolumePlan(
            "refuse", terminal=True,
            detail=(
                f"{device_link(name)} already holds a {found} filesystem "
                f"that Manifold has no record of writing. It will not mount "
                f"it (a job pointed at somebody else's data corrupts it) and "
                f"it will not format it (that destroys it). Use a volume "
                f"Manifold created, or move this disk's contents "
                f"yourself."))

    expected = recorded_fstype or DEFAULT_FSTYPE
    if found != expected:
        return VolumePlan(
            "refuse", terminal=True,
            detail=(
                f"{device_link(name)} holds a {found} filesystem but volume "
                f"'{name}' is recorded as {expected}. Manifold is looking at "
                f"a different disk than it thinks, so it will not mount "
                f"anything at {path}."))
    return VolumePlan("mount", fstype=found,
                      detail=f"{device_link(name)} holds the recorded "
                             f"{found} filesystem")
