"""User preferences: the policies you can change from the Settings page.

Three sources of truth, kept strictly apart (see CLAUDE.md):
- .env         secrets. Never here.
- config.yaml  tunables shipped with the app; supplies the DEFAULTS below.
- preferences  the user's own runtime choices, stored in SQLite.

config.yaml is a file a person edits with an editor and comments; a UI must
not rewrite it (it would eat the comments and the ordering). So the Settings
page writes here instead, and a stored preference overrides the config
default. Anything the user never touched simply falls through to config.

Three policies live here, and they exist for one reason: an UNATTENDED run
must be safe with nobody watching.

approvals    which agent actions pause for a human Approve/Deny.
notifications  what pings you, so a pause is not a silent stall.
data_safety  what happens to files on an instance when it is torn down.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from typing import Any

# The agent actions that can be gated. Kept next to the policy so the API,
# the UI, and the agent all read the same list.
GATEABLE_ACTIONS = ("launch_gpu", "run_job", "terminate_instance")

# Notification kinds. Each is an independent toggle.
NOTIFICATION_KINDS = (
    "approval_requested",   # an agent action is paused, waiting on you
    "job_succeeded",
    "job_failed",
    "run_finished",         # an autopilot run ended (any outcome)
    "data_transferred",     # files were rescued off an instance
    "capacity_available",   # a capacity watch found its GPU
    "instance_idle",        # a running instance is billing while barely used
    "instance_ceiling",     # an instance is near, or past, its max lifetime
    "budget_threshold",     # month-to-date spend crossed a share of the budget
    "terminal_reaped",      # a detached shell was killed after its grace period
    "bootstrap_failed",     # a launch's bootstrap script exited nonzero
)


@dataclass(frozen=True)
class ApprovalPrefs:
    """Which autopilot actions require a human Approve/Deny.

    Defaults gate LAUNCHES ONLY, and that is a money decision, not a
    timidity one. An approval that nobody answers auto-denies after
    autopilot.approval_timeout_seconds — so gating an action means "if I am
    away from the keyboard, this action does not happen".

    - launch_gpu:         denied by absence = no GPU starts = $0. Safe to gate.
    - terminate_instance: denied by absence = the GPU KEEPS BILLING while the
                          approval sits unread. Gating this actively loses
                          money when you are away, which is exactly when
                          autopilot is running. Off by default.
    - run_job:            denied by absence = an already-billing GPU sits idle.
                          Off by default for the same reason.
    """
    launch_gpu: bool = True
    run_job: bool = False
    terminate_instance: bool = False

    def gated_actions(self) -> frozenset[str]:
        return frozenset(a for a in GATEABLE_ACTIONS if getattr(self, a))


@dataclass(frozen=True)
class NotificationPrefs:
    approval_requested: bool = True
    job_succeeded: bool = True
    job_failed: bool = True
    run_finished: bool = True
    data_transferred: bool = True
    # A watch without auto-launch is ONLY a notification: if this is off,
    # capacity comes and goes silently and the watch was pointless.
    capacity_available: bool = True
    # An instance billing at full rate while its GPUs sit idle. This one is
    # the whole feature: idle spend you have not noticed is the money this
    # tool exists to save, and the notification never terminates anything -
    # it tells you, and you decide.
    instance_idle: bool = True
    # The max-lifetime ceiling: a heads-up before it fires, and the two cases
    # where it fired and Manifold deliberately did NOT destroy the box (it
    # was unreachable, or a job's own teardown is stuck). Separate from
    # instance_idle because switching off idle-spend chatter must not also
    # switch off the warning that a box is about to be terminated.
    instance_ceiling: bool = True
    # Month-to-date spend crossing a share of the monthly budget. Advisory
    # only: the budget never blocks a launch (see GuardrailPrefs), so this
    # notification IS the feature - a cap you are not told about is a cap
    # that does nothing.
    budget_threshold: bool = True
    # A detached terminal shell reaped after its grace period. The shell may
    # have had an agent session running in it, and killing it is the only
    # destructive act in Manifold that used to happen with no record at all.
    # On by default: a silent kill is how "my chat history disappeared"
    # becomes a mystery instead of a fact with a timestamp.
    terminal_reaped: bool = True
    # A launch's bootstrap script exited nonzero. On by default because the
    # box does NOT get terminated for it (a typo in an apt line must not
    # destroy work already on the disk), so this ping is the only thing
    # standing between "the setup failed" and an agent later finding half a
    # machine and no explanation.
    bootstrap_failed: bool = True
    # Also raise an OS notification (macOS Notification Center, libnotify on
    # Linux). In-app notifications are always recorded regardless; this only
    # controls the ping outside the window.
    desktop: bool = True

    def wants(self, kind: str) -> bool:
        return bool(getattr(self, kind, False))


@dataclass(frozen=True)
class DataSafetyPrefs:
    """What happens to files living on an instance when it is torn down.

    An instance's scratch disk dies with the instance. Manifold already
    refuses to terminate while valuable unpersisted files exist (the Phase 3
    safety hook), but "refuse" is only a good answer when a human is
    watching: an autopilot run at 3am would just leave the GPU billing.

    So termination now RESCUES first, then re-checks. Two independent
    questions, deliberately not collapsed into one menu:

      WHERE does the data go?   to_filesystem (the Lambda persistent volume,
                                a datacenter-local rsync, effectively free)
                                and/or to_local (down the SSH connection to
                                this machine, which costs real transfer time).
      WHAT is worth rescuing?   scope: "all" unpersisted files, or "outputs"
                                only (files under outputs/ — what jobs write
                                as their deliverable, not multi-GB checkpoints
                                and caches).

    And the one the four-option menu misses: WHAT IF THE RESCUE FAILS.
    if_unsaveable decides between the data and the wallet when a file could
    not be saved (no filesystem attached, transfer budget exhausted, SSH
    down): "block" keeps the instance alive with the files intact and pings
    you; "terminate" stops the billing and lets the files die. Default is
    block, because data loss is unrecoverable and a billing hour is not.
    """
    to_filesystem: bool = True
    to_local: bool = False
    scope: str = "all"                       # "all" | "outputs"
    local_dir: str = "~/Manifold/rescued"
    # Ceiling on ONE instance's download to this machine. A 40 GB checkpoint
    # dragged over a home connection would bill the GPU for the whole
    # transfer; files that do not fit are skipped and reported, never
    # silently dropped.
    max_local_gib: float = 25.0
    if_unsaveable: str = "block"             # "block" | "terminate"


@dataclass(frozen=True)
class GuardrailPrefs:
    """User-editable spending guardrails, from the Settings page.

    0 means "not set here - use the config.yaml default". The guards
    themselves stay in the orchestrator (hard rule); this only decides the
    NUMBERS they enforce, so raising the instance limit never needs a YAML
    edit and a backend restart.
    """
    max_concurrent_instances: int = 0
    max_hourly_spend_usd: float = 0.0
    # A CUMULATIVE wallet for the calendar month, and a different kind of
    # number from the two above: those are RATE ceilings the orchestrator
    # enforces before a launch, this one is only ever reported.
    #
    # It is deliberately advisory. Month-to-date is reconstructed from the
    # launches Manifold started, so it is a LOWER BOUND by construction -
    # an instance launched from the provider's own console has no launch row
    # and is invisible to it. Blocking a launch on a number we know is short
    # would refuse work without protecting the wallet, and it would decide
    # from our own database, which is exactly the evidence _guard_capacity
    # deliberately refuses to trust. So: warn loudly, never refuse.
    monthly_budget_usd: float = 0.0


@dataclass(frozen=True)
class ProviderPrefs:
    """Which cloud a launch lands on when the caller does not name one.

    The account's answer to "which cloud is this project on", in ONE place.
    Every client that omits a provider (the MCP bridge, an agent, the launch
    form on first paint) follows it, so switching clouds when Lambda credits
    run low is one toggle here instead of an edit to every agent's habits.

    Deliberately NOT validated in this module: a legal value is a provider
    the running backend actually registered, and preferences.py has no
    registry. The PUT /preferences route checks the name against
    orchestrator.providers and refuses an unknown one with the list of
    registered names; the orchestrator checks again at launch time, because
    a provider can be de-registered after the choice was stored.

    An explicit provider on the request always wins - this is the default,
    not a policy. The background loops (auto-manage, capacity watches,
    autopilot) name their provider explicitly for exactly that reason: a
    job queued for a Lambda GPU must not change clouds under a toggle.
    """
    default_provider: str = "lambda"


@dataclass(frozen=True)
class WorklogPrefs:
    """Where the worklog is mirrored, beyond the always-on copy in the data
    dir. Point mirror_dir at an Obsidian vault or a repo and every agent
    working there can read what Manifold accomplished. Empty = no mirror."""
    mirror_dir: str = ""


@dataclass(frozen=True)
class OnboardingPrefs:
    """Whether the first-run walkthrough has been finished or dismissed.

    Server-side on purpose. localStorage would answer this differently in the
    desktop shell and in a browser pointed at the same backend, so the same
    install would greet you twice or not at all.
    """
    completed: bool = False
    dismissed_at: str = ""      # ISO 8601 UTC, or "" if never dismissed


@dataclass(frozen=True)
class Preferences:
    approvals: ApprovalPrefs = ApprovalPrefs()
    notifications: NotificationPrefs = NotificationPrefs()
    data_safety: DataSafetyPrefs = DataSafetyPrefs()
    guardrails: GuardrailPrefs = GuardrailPrefs()
    providers: ProviderPrefs = ProviderPrefs()
    worklog: WorklogPrefs = WorklogPrefs()
    onboarding: OnboardingPrefs = OnboardingPrefs()

    def to_dict(self) -> dict:
        return asdict(self)


def _coerce(section, raw: dict):
    """Apply a raw dict onto a frozen prefs dataclass, ignoring unknown keys
    and keeping each field's declared type. Unknown/garbage values keep the
    current value rather than raising — a preferences file must never be able
    to stop the backend from starting."""
    updates: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        if not hasattr(section, key):
            continue
        current = getattr(section, key)
        try:
            if isinstance(current, bool):
                updates[key] = bool(value)
            elif isinstance(current, int):
                updates[key] = int(value)
            elif isinstance(current, float):
                updates[key] = float(value)
            elif isinstance(current, str):
                updates[key] = str(value)
        except (TypeError, ValueError):
            continue
    section = replace(section, **updates)
    return _validate(section)


def _validate(section):
    """Clamp the enumerated fields to legal values."""
    if isinstance(section, DataSafetyPrefs):
        fixes = {}
        if section.scope not in ("all", "outputs"):
            fixes["scope"] = "all"
        if section.if_unsaveable not in ("block", "terminate"):
            fixes["if_unsaveable"] = "block"
        if section.max_local_gib < 0:
            fixes["max_local_gib"] = 0.0
        if fixes:
            section = replace(section, **fixes)
    if isinstance(section, GuardrailPrefs):
        fixes = {}
        if section.max_concurrent_instances < 0:
            fixes["max_concurrent_instances"] = 0
        if section.max_hourly_spend_usd < 0:
            fixes["max_hourly_spend_usd"] = 0.0
        if section.monthly_budget_usd < 0:
            fixes["monthly_budget_usd"] = 0.0
        if fixes:
            section = replace(section, **fixes)
    return section


def preferences_from_dict(base: Preferences, raw: dict) -> Preferences:
    """Overlay a (possibly partial, possibly hostile) dict onto `base`."""
    raw = raw or {}
    return Preferences(
        approvals=_coerce(base.approvals, raw.get("approvals", {})),
        notifications=_coerce(base.notifications, raw.get("notifications", {})),
        data_safety=_coerce(base.data_safety, raw.get("data_safety", {})),
        guardrails=_coerce(base.guardrails, raw.get("guardrails", {})),
        providers=_coerce(base.providers, raw.get("providers", {})),
        worklog=_coerce(base.worklog, raw.get("worklog", {})),
        onboarding=_coerce(base.onboarding, raw.get("onboarding", {})),
    )


class PreferenceStore:
    """Reads/writes the preferences row; every component reads through this
    so a change from the Settings page takes effect on the next tick with no
    restart and no object graph to re-thread."""

    KEY = "preferences"

    def __init__(self, db, defaults: Preferences | None = None):
        self._db = db
        self._defaults = defaults or Preferences()
        self._cached: Preferences | None = None

    def get(self) -> Preferences:
        if self._cached is None:
            raw = self._db.get_preferences(self.KEY)
            self._cached = preferences_from_dict(self._defaults, raw or {})
        return self._cached

    def update(self, patch: dict) -> Preferences:
        """Merge a partial patch over the CURRENT preferences and persist."""
        merged = preferences_from_dict(self.get(), patch)
        self._db.set_preferences(self.KEY, json.loads(json.dumps(merged.to_dict())))
        self._cached = merged
        return merged

    def reset(self) -> Preferences:
        self._db.set_preferences(self.KEY, {})
        self._cached = self._defaults
        return self._defaults
