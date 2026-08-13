"""Launch policy as code (Phase 82).

policy.yaml is the reviewable half of the guardrails: WHICH instance
types, WHICH regions, at WHAT per-instance rate, with WHAT lifetime
discipline - per role, binding every principal INCLUDING the owner. The
numeric dials in config.yaml (concurrency, total hourly budget) are the
owner's own limits; this file is the one a team reviews in a pull
request, and the only way around it is a commit.

Failure semantics are asymmetric on purpose:
- MISSING file: fully permissive. A fresh install is not policied.
- PRESENT but invalid: refuse to boot (SystemExit). A guard that fails
  open because of a typo is a hole shaped exactly like a guard.

Decisions are pure (no I/O); loading lives at the bottom, mirroring the
config loader. The orchestrator asks `allows_launch` and turns a denial
into a LaunchRejected - policy never has its own enforcement path.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Keys accepted at each level. Unknown keys REJECT the file rather than
# silently not constraining anything: a typo'd "alowed_regions" that
# loads as permissive is the failure mode this module exists to prevent.
_RULE_KEYS = frozenset({
    "allowed_instance_types", "allowed_regions",
    "max_hourly_rate_usd", "require_max_lifetime",
})
_TOP_KEYS = frozenset({"version", "launch", "roles"})
_ROLE_NAMES = frozenset({"viewer", "operator", "admin"})


class PolicyError(Exception):
    """The file exists but cannot be trusted (parse error, unknown keys,
    wrong types). The caller turns this into a refusal to boot."""


@dataclass(frozen=True)
class RuleSet:
    """One block of constraints. Empty lists / zeros mean 'no opinion'."""
    allowed_instance_types: tuple[str, ...] = ()
    allowed_regions: tuple[str, ...] = ()
    max_hourly_rate_usd: float = 0.0
    require_max_lifetime: bool = False


@dataclass(frozen=True)
class Policy:
    source: str = "(none)"            # where this policy came from
    active: bool = False              # False = the permissive default
    launch: RuleSet = field(default_factory=RuleSet)
    roles: dict[str, RuleSet] = field(default_factory=dict)

    def allows_launch(self, *, instance_type: str, region: str,
                      hourly_rate_usd: float,
                      max_lifetime_seconds: float | None,
                      role: str | None) -> str | None:
        """None when the launch is allowed, else the denial reason.

        Role rules TIGHTEN the global block, never widen it: both must
        pass, the rate cap is the smaller of the two, and the lifetime
        requirement is either's. An unknown/absent role (open mode,
        legacy actors) is bound by the global block alone."""
        blocks = [("policy", self.launch)]
        if role and role in self.roles:
            blocks.append((f"policy for role '{role}'", self.roles[role]))
        for label, rules in blocks:
            if not _matches(instance_type, rules.allowed_instance_types):
                return (
                    f"{label} does not allow instance type "
                    f"'{instance_type}' (allowed: "
                    f"{', '.join(rules.allowed_instance_types)})")
            if not _matches(region, rules.allowed_regions):
                return (f"{label} does not allow region '{region}' "
                        f"(allowed: {', '.join(rules.allowed_regions)})")
            if (rules.max_hourly_rate_usd
                    and hourly_rate_usd > rules.max_hourly_rate_usd):
                return (
                    f"{label} caps per-instance rate at "
                    f"${rules.max_hourly_rate_usd:.2f}/hr; "
                    f"'{instance_type}' costs ${hourly_rate_usd:.2f}/hr")
            if rules.require_max_lifetime and max_lifetime_seconds is None:
                return (
                    f"{label} requires a max lifetime on every launch "
                    f"(set one, or launch from a job/template that does)")
        return None


def _matches(value: str, patterns: tuple[str, ...]) -> bool:
    """Empty patterns = no opinion = allowed. fnmatch, so 'gpu_1x_*'
    reads the way an ops reviewer expects."""
    return not patterns or any(fnmatch.fnmatch(value, p) for p in patterns)


PERMISSIVE = Policy()


def _parse_rules(raw: dict, where: str) -> RuleSet:
    unknown = set(raw) - _RULE_KEYS
    if unknown:
        raise PolicyError(
            f"unknown key(s) in {where}: {', '.join(sorted(unknown))}. "
            f"Known keys: {', '.join(sorted(_RULE_KEYS))}. A typo here "
            f"would silently constrain nothing, so it refuses instead.")
    types = raw.get("allowed_instance_types") or []
    regions = raw.get("allowed_regions") or []
    if (not isinstance(types, list)
            or not all(isinstance(t, str) for t in types)):
        raise PolicyError(f"{where}.allowed_instance_types must be a "
                          f"list of strings")
    if (not isinstance(regions, list)
            or not all(isinstance(r, str) for r in regions)):
        raise PolicyError(f"{where}.allowed_regions must be a list of "
                          f"strings")
    rate = raw.get("max_hourly_rate_usd", 0)
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or rate < 0:
        raise PolicyError(f"{where}.max_hourly_rate_usd must be a "
                          f"non-negative number")
    require = raw.get("require_max_lifetime", False)
    if not isinstance(require, bool):
        raise PolicyError(f"{where}.require_max_lifetime must be true "
                          f"or false")
    return RuleSet(
        allowed_instance_types=tuple(types),
        allowed_regions=tuple(regions),
        max_hourly_rate_usd=float(rate),
        require_max_lifetime=require,
    )


def load_policy(path: Path) -> Policy:
    """The file at `path`, parsed strictly; PERMISSIVE when it does not
    exist. Raises PolicyError on anything untrustworthy."""
    if not path.exists():
        return PERMISSIVE
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise PolicyError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError(f"{path} must be a YAML mapping")
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        raise PolicyError(
            f"unknown top-level key(s) in {path}: "
            f"{', '.join(sorted(unknown))}. Known: version, launch, roles.")
    if raw.get("version", 1) != 1:
        raise PolicyError(
            f"{path} declares version {raw.get('version')!r}; this "
            f"Manifold understands version 1")
    roles_raw = raw.get("roles") or {}
    if not isinstance(roles_raw, dict):
        raise PolicyError(f"{path}: roles must be a mapping of role name "
                          f"to rules")
    unknown_roles = set(roles_raw) - _ROLE_NAMES
    if unknown_roles:
        raise PolicyError(
            f"{path}: unknown role(s) {', '.join(sorted(unknown_roles))}. "
            f"Roles: {', '.join(sorted(_ROLE_NAMES))}.")
    return Policy(
        source=str(path),
        active=True,
        launch=_parse_rules(raw.get("launch") or {}, "launch"),
        roles={name: _parse_rules(block or {}, f"roles.{name}")
               for name, block in roles_raw.items()},
    )


def describe(policy: Policy) -> dict:
    """The policy as a JSON-shaped dict for GET /policy - what is
    ENFORCED, from where, with no yaml round-tripping."""
    def rules(r: RuleSet) -> dict:
        return {
            "allowed_instance_types": list(r.allowed_instance_types),
            "allowed_regions": list(r.allowed_regions),
            "max_hourly_rate_usd": r.max_hourly_rate_usd,
            "require_max_lifetime": r.require_max_lifetime,
        }
    return {
        "active": policy.active,
        "source": policy.source,
        "launch": rules(policy.launch),
        "roles": {name: rules(r) for name, r in policy.roles.items()},
    }
