
## Phase 82 — Policy as code: the guardrail you review in a pull request (2026-08-13)

- **policy.yaml constrains WHICH; config.yaml keeps HOW MUCH.** The numeric
  dials (concurrency, total hourly budget) are the owner's own limits and
  stay where they were. The policy file is the half a TEAM reviews:
  instance-type and region allowlists (fnmatch, so `gpu_1x_*` reads the way
  an ops reviewer expects), a per-instance rate cap, a required max
  lifetime - globally and per role. Role blocks TIGHTEN the global block
  and can never widen it: both must pass. It binds every principal
  INCLUDING the owner; the way around policy.yaml is a commit to
  policy.yaml, which is the entire point.

- **Asymmetric failure semantics, on purpose.** Missing file = fully
  permissive (a fresh install is not policied). Present-but-invalid =
  refuse to boot, unknown keys included: a typo'd `alowed_regions` that
  loaded as "no opinion" would be a hole shaped exactly like a guard.
  This deliberately breaks the config-loader convention that garbage can
  never stop the backend - preferences are conveniences, policy is a
  guard, and the two failing the same way would be the wrong symmetry.

- **Deliberately not editable from the dashboard.** GET /policy (and the
  Settings card) report what is enforced and from which file; changing it
  is an edit plus a restart. An API that could rewrite the policy would
  collapse the one property that distinguishes it from preferences: that
  every change has a diff, an author, and a reviewer.

- **Enforced in the orchestrator, before any money-side guard.** A denial
  costs nothing, names its rule and its file, and lands in the audit
  trail with the principal's name (enterprise question: who ASKED for
  the thing policy refused). Fallback instance types pass the same
  policy the requested type passed - a fallback that policy denies is
  skipped, exactly like one the budget denies. Clusters are judged
  before the cluster row is created (no ghost rows), and a
  require_max_lifetime policy honestly denies clusters until clusters
  learn lifetime ceilings - recorded here rather than silently exempted.

**Test coverage:** `tests/test_policy.py` - the pure engine (patterns,
rate cap, lifetime requirement, role tightening incl. "a role block can
never re-allow"), loading (missing permissive; nine invalid shapes each
refusing with the offending fragment named), enforcement over HTTP (the
owner denied and audited, role rules biting a minted operator then
admitting with a lifetime, region denial isolated from the
filesystem-region validation via a scratch-only launch, cluster denied
with no row left behind), GET /policy and the settings flag. Full suite
839 passing; dashboard builds clean; live probe confirmed the owner
denial naming the file, the operator lifetime rule, a compliant 202,
and a typo'd policy refusing to boot with the unknown key named.
