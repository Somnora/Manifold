"""Phase 82: policy as code.

policy.yaml is the reviewable half of the guardrails, enforced in the
orchestrator against everyone including the owner. The failure semantics
are the design: a missing file is permissive, an INVALID file refuses to
boot, and unknown keys are errors - a typo that silently constrained
nothing would be a hole shaped exactly like a guard.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import Guardrails
from app.main import create_app
from app.policy import (
    PERMISSIVE,
    Policy,
    PolicyError,
    RuleSet,
    describe,
    load_policy,
)
from tests.conftest import make_settings, mock_connect_fn

OWNER = "owner-token-for-tests"


# -- the pure engine ----------------------------------------------------------


def allows(policy, **kw):
    defaults = dict(instance_type="gpu_1x_a10", region="us-east-1",
                    hourly_rate_usd=1.29, max_lifetime_seconds=None,
                    role=None)
    defaults.update(kw)
    return policy.allows_launch(**defaults)


def test_permissive_allows_everything():
    assert allows(PERMISSIVE) is None
    assert allows(PERMISSIVE, instance_type="gpu_8x_h100_sxm5",
                  hourly_rate_usd=999.0, role="viewer") is None


def test_type_and_region_patterns():
    p = Policy(active=True, launch=RuleSet(
        allowed_instance_types=("gpu_1x_*",),
        allowed_regions=("us-east-1", "us-west-*")))
    assert allows(p) is None
    assert allows(p, region="us-west-2") is None
    denial = allows(p, instance_type="gpu_8x_h100_sxm5")
    assert "gpu_8x_h100_sxm5" in denial and "gpu_1x_*" in denial
    assert "region" in allows(p, region="eu-central-1")


def test_rate_cap_and_lifetime_requirement():
    p = Policy(active=True, launch=RuleSet(
        max_hourly_rate_usd=2.0, require_max_lifetime=True))
    assert "max lifetime" in allows(p)
    assert allows(p, max_lifetime_seconds=7200.0) is None
    denial = allows(p, hourly_rate_usd=24.72, max_lifetime_seconds=7200.0)
    assert "$2.00" in denial and "$24.72" in denial


def test_role_rules_tighten_never_widen():
    p = Policy(
        active=True,
        launch=RuleSet(allowed_instance_types=("gpu_1x_*", "gpu_8x_*")),
        roles={"operator": RuleSet(allowed_instance_types=("gpu_1x_*",),
                                   max_hourly_rate_usd=1.50)},
    )
    # Admin (no role block) gets the global rules.
    assert allows(p, instance_type="gpu_8x_h100_sxm5", role="admin") is None
    # Operator is tightened: the role block also has to pass.
    denial = allows(p, instance_type="gpu_8x_h100_sxm5", role="operator")
    assert "role 'operator'" in denial
    assert "rate" in allows(p, hourly_rate_usd=1.60, role="operator")
    assert allows(p, role="operator") is None
    # A role block can never re-allow what the global block denies.
    p2 = Policy(active=True,
                launch=RuleSet(allowed_instance_types=("gpu_1x_*",)),
                roles={"admin": RuleSet()})
    assert allows(p2, instance_type="gpu_8x_h100_sxm5",
                  role="admin") is not None


# -- loading: missing is permissive, invalid refuses --------------------------


def test_missing_file_is_permissive(tmp_path):
    p = load_policy(tmp_path / "policy.yaml")
    assert p.active is False
    assert allows(p, hourly_rate_usd=999.0) is None


def test_valid_file_loads(tmp_path):
    f = tmp_path / "policy.yaml"
    f.write_text(
        "version: 1\n"
        "launch:\n"
        "  allowed_instance_types: [gpu_1x_*]\n"
        "  max_hourly_rate_usd: 3.0\n"
        "roles:\n"
        "  operator:\n"
        "    require_max_lifetime: true\n")
    p = load_policy(f)
    assert p.active is True and p.source == str(f)
    assert p.launch.allowed_instance_types == ("gpu_1x_*",)
    assert p.roles["operator"].require_max_lifetime is True
    # describe() round-trips to the API shape.
    doc = describe(p)
    assert doc["active"] and doc["roles"]["operator"]["require_max_lifetime"]


@pytest.mark.parametrize("content,fragment", [
    ("version: 2\n", "version"),
    ("launch:\n  alowed_regions: [us-east-1]\n", "alowed_regions"),
    ("launch:\n  max_hourly_rate_usd: cheap\n", "non-negative number"),
    ("launch:\n  max_hourly_rate_usd: -1\n", "non-negative number"),
    ("launch:\n  require_max_lifetime: sometimes\n", "true or false"),
    ("roles:\n  superuser: {}\n", "superuser"),
    ("surprise: {}\n", "surprise"),
    ("[not, a, mapping]\n", "mapping"),
    ("launch: {allowed_instance_types: gpu_1x_a10}\n", "list of strings"),
])
def test_invalid_files_refuse(tmp_path, content, fragment):
    f = tmp_path / "policy.yaml"
    f.write_text(content)
    with pytest.raises(PolicyError) as exc:
        load_policy(f)
    assert fragment in str(exc.value)


# -- enforcement in the orchestrator ------------------------------------------


def app_with(tmp_path, mock_client, mock_storage, mock_sidecar, policy):
    return create_app(
        make_settings(tmp_path, api_token=OWNER,
                      guardrails=Guardrails(max_concurrent_instances=5,
                                            max_hourly_spend_usd=50.0)),
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        env_path=tmp_path / ".env",
        policy=policy,
    )


@pytest.fixture
def restricted(tmp_path, mock_client, mock_storage, mock_sidecar):
    """gpu_1x only, us-east-1 only; operators additionally rate-capped
    at $1.50/hr and required to set a lifetime."""
    policy = Policy(
        active=True, source="test-policy.yaml",
        launch=RuleSet(allowed_instance_types=("gpu_1x_*",),
                       allowed_regions=("us-east-1",)),
        roles={"operator": RuleSet(max_hourly_rate_usd=1.50,
                                   require_max_lifetime=True)},
    )
    app = app_with(tmp_path, mock_client, mock_storage, mock_sidecar,
                   policy)
    with TestClient(app,
                    headers={"Authorization": f"Bearer {OWNER}"}) as c:
        yield c


def launch_body(**kw):
    body = {"instance_type": "gpu_1x_a10", "region": "us-east-1",
            "filesystem": "manifold-data"}
    body.update(kw)
    return body


def test_policy_binds_the_owner_too(restricted):
    resp = restricted.post("/instances",
                           json=launch_body(instance_type="gpu_8x_h100_sxm5"))
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert "policy" in detail and "test-policy.yaml" in detail
    # And the denial is in the audit trail with the principal's name.
    db = restricted.app.state.orchestrator.db
    rows = [r for r in db.list_audit() if r["action"] == "policy_denied"]
    assert rows and rows[0]["actor"] == "owner"


def test_policy_role_rules_bind_minted_principals(restricted):
    resp = restricted.post("/principals", json={"name": "worker",
                                                "role": "operator"})
    worker = TestClient(restricted.app)
    worker.headers.update(
        {"Authorization": f"Bearer {resp.json()['token']}"})
    # $1.29 is under the operator cap, but the lifetime requirement bites.
    denied = worker.post("/instances", json=launch_body())
    assert denied.status_code == 403
    assert "max lifetime" in denied.json()["detail"]
    ok = worker.post("/instances",
                     json=launch_body(max_lifetime_seconds=7200))
    assert ok.status_code == 202
    # The owner (admin, no role block) launches without a lifetime.
    assert restricted.post(
        "/instances", json=launch_body()).status_code == 202


def test_allowed_launch_passes_and_region_denies(restricted):
    assert restricted.post(
        "/instances", json=launch_body()).status_code == 202
    # Scratch-only (no filesystem), so the filesystem's region-lock
    # validation cannot fire first: what denies here is POLICY.
    resp = restricted.post(
        "/instances", json=launch_body(region="us-east-2", filesystem=""))
    assert resp.status_code == 403
    assert "region" in resp.json()["detail"]


def test_cluster_denied_before_any_row(restricted):
    resp = restricted.post("/clusters/launch", json={
        "instance_type": "gpu_8x_h100_sxm5", "region": "us-east-1",
        "filesystem": "manifold-data", "node_count": 2})
    assert resp.status_code == 403
    assert restricted.get("/clusters").json()["clusters"] == []


def test_get_policy_reports_what_is_enforced(restricted):
    doc = restricted.get("/policy").json()
    assert doc["active"] is True
    assert doc["launch"]["allowed_instance_types"] == ["gpu_1x_*"]
    assert doc["roles"]["operator"]["max_hourly_rate_usd"] == 1.50
    assert restricted.get(
        "/settings/status").json()["policy_active"] is True


def test_no_policy_reports_inactive(client):
    doc = client.get("/policy").json()
    assert doc["active"] is False
    assert client.get("/settings/status").json()["policy_active"] is False
