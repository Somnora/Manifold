"""Phase 102: the account's default provider, and everything it must NOT move.

The feature is one sentence: say "this project runs on GCP" once, and every
client that does not name a cloud follows - no agent has to learn anything.
Which makes the interesting tests the ones about what a flip may not touch:
a job queued for a Lambda GPU, a watch on the Lambda catalog, an autopilot
run picking from the Lambda ladder, and a cluster.

Everything runs against mocks. The GCP provider that create_app registers in
tests is the REAL one with no project id (main.py registers 'gcp' either way),
which is exactly the unconfigured case one test wants; the others swap the
mock GCP provider in behind the same registered name.
"""

import pytest
from fastapi.testclient import TestClient

import app.mcp_server as mcp_server
from app.config import Guardrails
from app.providers.base import ProviderError, ProviderUnavailable
from app.providers.gcp_provider import MockGCPProvider
from tests.conftest import wait_for_launch_status

# A target the mock GCP catalog can actually satisfy. Nothing about it is
# valid on Lambda, which is the point: a launch that lands on the wrong
# cloud fails on the instance type, loudly.
GCP_TARGET = {"instance_type": "g2-standard-4", "region": "us-central1",
              "filesystem": ""}
LAMBDA_TARGET = {"instance_type": "gpu_1x_a10", "region": "us-east-1",
                 "filesystem": "manifold-data"}


def use_mock_gcp(client) -> MockGCPProvider:
    """Swap the registered-but-unconfigured GCP provider for the mock one.

    create_app already registers 'gcp' (unconfigured in tests), so this
    replaces the object behind a name the resolver will look up either way -
    the same seam tests/test_reconcile.py builds registries with.
    """
    provider = MockGCPProvider()
    client.app.state.orchestrator.providers.register("gcp", provider)
    return provider


def set_default_provider(client, name: str):
    resp = client.put("/preferences", json={"providers":
                                            {"default_provider": name}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["preferences"]["providers"]["default_provider"] == name


def launch_row(client, launch_id: str) -> dict:
    resp = client.get(f"/launches/{launch_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


# -- the default itself ---------------------------------------------------------


def test_shipped_default_is_lambda_and_an_omitted_provider_still_gets_it(
    client, mock_client
):
    """The pin on today's behaviour: a client that never names a provider
    (every pre-Phase-102 bridge) lands on Lambda exactly as before."""
    assert client.get("/preferences").json()[
        "preferences"]["providers"]["default_provider"] == "lambda"

    resp = client.post("/instances", json=LAMBDA_TARGET)
    assert resp.status_code == 202, resp.text
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    assert launch["status"] == "active"
    assert launch["provider"] == "lambda"
    assert len(mock_client.launch_calls) == 1


def test_flip_routes_an_omitted_provider_to_the_new_cloud(client, mock_client):
    gcp = use_mock_gcp(client)
    set_default_provider(client, "gcp")

    resp = client.post("/instances", json=GCP_TARGET)
    assert resp.status_code == 202, resp.text
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    assert launch["status"] == "active", launch.get("error")
    # The ROW carries the resolved name, never null: every later reader (boot
    # poll, reconcile sweep, spend attribution) asks the row which cloud.
    assert launch["provider"] == "gcp"
    assert len(gcp.instances) == 1
    # ...and Lambda was never called.
    assert mock_client.launch_calls == []


def test_an_explicit_provider_always_beats_the_default(client, mock_client):
    """Both directions: the default never overrides a caller who said which
    cloud they meant."""
    gcp = use_mock_gcp(client)
    set_default_provider(client, "gcp")

    resp = client.post("/instances", json={**LAMBDA_TARGET,
                                           "provider": "lambda"})
    assert resp.status_code == 202, resp.text
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    assert launch["provider"] == "lambda"
    assert len(mock_client.launch_calls) == 1
    assert gcp.instances == {}


def test_explicit_gcp_under_a_lambda_default(client, mock_client):
    gcp = use_mock_gcp(client)
    resp = client.post("/instances", json={**GCP_TARGET, "provider": "gcp"})
    assert resp.status_code == 202, resp.text
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    assert launch["provider"] == "gcp"
    assert len(gcp.instances) == 1
    assert mock_client.launch_calls == []


def test_unknown_provider_is_refused_at_the_preferences_route(client):
    """Refused, not clamped: a silently-ignored write reads as saved and
    then sends every default launch to the old cloud."""
    resp = client.put("/preferences",
                      json={"providers": {"default_provider": "azure"}})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "azure" in detail
    # The refusal names what WOULD work.
    assert "gcp" in detail and "lambda" in detail
    # And nothing was stored.
    assert client.get("/preferences").json()[
        "preferences"]["providers"]["default_provider"] == "lambda"


def test_registered_providers_are_published_for_the_settings_control(client):
    """The Settings control renders from this list instead of hardcoding
    names, so it can never offer a cloud the launch path would refuse."""
    assert client.get("/preferences").json()["registered_providers"] == [
        "gcp", "lambda"]


# -- what a flip must not move --------------------------------------------------


def test_auto_managed_job_stays_on_lambda_after_a_flip(tmp_path):
    """A job queued against a Lambda gpu_type, region and filesystem must
    launch on Lambda no matter what the account default says afterwards."""
    from app.sidecar_client import MockSidecarClient
    from tests.test_auto_manage import _app, _fast, _queue_auto, _wait_lifecycle

    app, mock = _app(_fast(tmp_path), sidecar=MockSidecarClient(unpersisted=[]))
    with TestClient(app) as client:
        gcp = use_mock_gcp(client)
        set_default_provider(client, "gcp")

        task_id = _queue_auto(client).json()["task"]["id"]
        task = _wait_lifecycle(
            client, task_id,
            ("launching", "ready", "running", "syncing", "done", "failed"))
        assert task["launch_id"], task
        assert launch_row(client, task["launch_id"])["provider"] == "lambda"
        assert gcp.instances == {}
        assert len(mock.launch_calls) == 1


def test_capacity_watch_auto_launch_stays_on_lambda_after_a_flip(
    tmp_path, mock_storage, mock_sidecar
):
    """The watch polls the LAMBDA catalog and fires on Lambda capacity;
    spending that evidence on another cloud would be a non-sequitur."""
    from copy import deepcopy

    from app.lambda_api import DEFAULT_MOCK_TYPES, MockLambdaClient
    from tests.test_watches import make_watch_app, wait_until

    # The catalog is deep-copied because this test mutates a type's
    # capacity: MockLambdaClient shallow-copies the module-level
    # DEFAULT_MOCK_TYPES, so editing an entry in place would follow the
    # process into every later test file.
    mock_client = MockLambdaClient(
        instance_types=deepcopy(DEFAULT_MOCK_TYPES))
    mock_client.instance_types["gpu_1x_a10"].regions_with_capacity = []
    app = make_watch_app(tmp_path, mock_client, mock_storage, mock_sidecar,
                         auto_launch_enabled=True)
    with TestClient(app) as client:
        gcp = use_mock_gcp(client)
        set_default_provider(client, "gcp")

        watch_id = client.post("/watches", json={
            **LAMBDA_TARGET, "auto_launch": True,
        }).json()["watch"]["id"]
        mock_client.instance_types["gpu_1x_a10"].regions_with_capacity = [
            "us-east-1"]

        wait_until(
            lambda: next(w for w in client.get("/watches").json()["watches"]
                         if w["id"] == watch_id)["status"] == "launched",
            message="watch auto-launch",
        )
        assert len(mock_client.launch_calls) == 1
        assert gcp.instances == {}


def test_autopilot_launch_stays_on_lambda_after_a_flip(
    tmp_path, mock_client, mock_storage, mock_sidecar
):
    """The brain picks an instance type from the Lambda catalog it was
    shown; the toggle must not send that choice to a cloud it never saw."""
    from tests.test_autopilot import (
        ScriptedBrain, build_app, make_brain_instance, wait_run,
    )

    brain = ScriptedBrain([
        '{"action": "launch_gpu", "args": {"instance_type": "gpu_1x_a10",'
        ' "region": "us-east-1", "filesystem": "manifold-data"}}',
        '{"action": "done", "args": {"summary": "Launched an A10."}}',
    ])
    app = build_app(
        tmp_path, mock_client, mock_storage, mock_sidecar, brain,
        guardrails=Guardrails(max_concurrent_instances=2,
                              max_hourly_spend_usd=4.00),
    )
    with TestClient(app) as client:
        # The brain's own box launches BEFORE the flip, as it would in life.
        brain_id = make_brain_instance(client)
        gcp = use_mock_gcp(client)
        set_default_provider(client, "gcp")

        run_id = client.post("/autopilot/runs", json={
            "goal": "Launch an A10 in us-east-1.",
            "brain_instance_id": brain_id,
            "approve_actions": [],
        }).json()["run"]["id"]
        run = wait_run(client, run_id)

        assert run["status"] == "succeeded", run
        assert len(mock_client.launch_calls) == 2   # brain box + the new one
        assert gcp.instances == {}


def test_cluster_launch_stays_on_lambda(client, mock_client):
    """Clusters are deliberately explicit-lambda: nodes have to reach each
    other, and only the Lambda path is proven multi-node."""
    use_mock_gcp(client)
    set_default_provider(client, "gcp")

    resp = client.post("/clusters/launch", json={
        **LAMBDA_TARGET, "node_count": 1, "name": "pinned",
    })
    assert resp.status_code == 200, resp.text
    rows = client.get("/launches").json()["launches"]
    assert rows and all(r["provider"] == "lambda" for r in rows)


# -- honest refusals ------------------------------------------------------------


def test_unconfigured_gcp_says_so_instead_of_offering_no_instance_types(client):
    """The empty-catalog trap: an unconfigured provider lists nothing, and
    'Valid types: ' sends the reader hunting a typo that is not there."""
    set_default_provider(client, "gcp")     # the real, project-less provider

    resp = client.post("/instances", json=GCP_TARGET)
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "not configured yet" in detail
    assert "gcloud auth application-default login" in detail
    assert "Valid types" not in detail


def test_a_default_naming_a_provider_this_backend_lacks_is_refused(client, db):
    """A stored preference outlives the registry that validated it (a
    de-registered provider, a hand-edited file). The refusal names the
    providers that do exist rather than 500ing from inside the registry."""
    db.set_preferences("preferences", {"providers":
                                       {"default_provider": "azure"}})
    client.app.state.orchestrator.prefs._cached = None

    resp = client.post("/instances", json=LAMBDA_TARGET)
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "azure" in detail and "gcp, lambda" in detail


class _UnreadableCatalog(MockGCPProvider):
    """A provider whose catalog cannot be READ - the expired-ADC case."""

    FIX = ("Your Google login has expired (ADC refresh tokens age out). "
           "Run `gcloud auth application-default login` to sign in again.")

    async def list_instance_types(self):
        raise ProviderUnavailable(self.FIX)


class _UnreadableFleet(MockGCPProvider):
    """Catalog fine, but the account itself cannot be listed."""

    async def list_instances(self, *, fresh: bool = False):
        raise ProviderUnavailable(_UnreadableCatalog.FIX)


class _RefusingProvider(MockGCPProvider):
    """A provider that answers, with a problem (quota, bad config)."""

    async def list_instance_types(self):
        raise ProviderError("GPU quota in us-central1 is 0. Request an "
                            "increase in the Cloud console.")


def test_a_provider_that_cannot_be_read_is_a_503_carrying_its_own_fix(client):
    client.app.state.orchestrator.providers.register("gcp",
                                                     _UnreadableCatalog())
    set_default_provider(client, "gcp")

    resp = client.post("/instances", json=GCP_TARGET)
    assert resp.status_code == 503, resp.text
    # The one command that fixes it survives the trip intact.
    assert resp.json()["detail"] == _UnreadableCatalog.FIX


def test_a_guard_that_cannot_see_the_fleet_refuses_rather_than_guessing_zero(
    client
):
    """The guard's baseline is live instances. 'I could not list them' must
    never be rounded down to 'none are running' - that is the reading that
    spends money."""
    client.app.state.orchestrator.providers.register("gcp", _UnreadableFleet())
    set_default_provider(client, "gcp")

    resp = client.post("/instances", json=GCP_TARGET)
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert "Cannot check what is already running" in detail
    assert "gcloud auth application-default login" in detail


def test_a_provider_error_is_a_502_with_the_providers_message(client):
    client.app.state.orchestrator.providers.register("gcp",
                                                     _RefusingProvider())
    resp = client.post("/instances", json={**GCP_TARGET, "provider": "gcp"})
    assert resp.status_code == 502, resp.text
    assert "GPU quota in us-central1 is 0" in resp.json()["detail"]


# -- launch options follow the flip ---------------------------------------------


def test_launch_options_are_lambda_and_say_so_by_default(client):
    body = client.get("/launch-options").json()
    assert body["provider"] == "lambda"
    assert body["targets"]
    assert {t["provider"] for t in body["targets"]} == {"lambda"}
    assert "unavailable_reason" not in body


def test_launch_options_follow_the_default_provider(client):
    use_mock_gcp(client)
    set_default_provider(client, "gcp")

    body = client.get("/launch-options").json()
    assert body["provider"] == "gcp"
    assert {t["provider"] for t in body["targets"]} == {"gcp"}
    assert {t["instance_type"] for t in body["targets"]} == {
        "g2-standard-4", "g2-standard-12", "a2-highgpu-1g", "n1-standard-8-t4"}
    # Persistent filesystems are a Lambda feature; every GCP target is
    # honestly scratch-only rather than pretending co-location.
    assert all(t["filesystem"] is None for t in body["targets"])
    # A target copied straight into a launch works, which is the whole
    # reason this route had to follow the toggle.
    top = body["targets"][0]
    resp = client.post("/instances", json={
        "instance_type": top["instance_type"], "region": top["region"],
        "filesystem": ""})
    assert resp.status_code == 202, resp.text


def test_launch_options_can_be_asked_about_another_provider(client):
    use_mock_gcp(client)
    set_default_provider(client, "gcp")

    body = client.get("/launch-options?provider=lambda").json()
    assert body["provider"] == "lambda"
    assert {t["provider"] for t in body["targets"]} == {"lambda"}

    unknown = client.get("/launch-options?provider=azure")
    assert unknown.status_code == 422, unknown.text
    assert "azure" in unknown.json()["detail"]


def test_launch_options_for_an_unconfigured_cloud_say_why_it_is_empty(client):
    """An empty target list is indistinguishable from 'no capacity anywhere'
    unless the reason is present."""
    body = client.get("/launch-options?provider=gcp").json()
    assert body["provider"] == "gcp"
    assert body["targets"] == []
    assert "not configured yet" in body["unavailable_reason"]


# -- the guard asymmetry, documented as intended --------------------------------


def test_live_instances_count_only_against_their_own_cloud(client, mock_client):
    """PINNED, not accidental: the live half of the concurrency baseline
    comes from the LAUNCHING provider alone, so a running Lambda box does
    not hold the single slot against a GCP launch. Asking every registered
    provider would let one unreadable cloud veto a launch on a working one.
    See Orchestrator._guard_capacity and the Phase 102 DECISIONS entry."""
    gcp = use_mock_gcp(client)
    first = client.post("/instances", json=LAMBDA_TARGET)
    wait_for_launch_status(client, first.json()["launch"]["id"])

    # A second LAMBDA launch is refused: same cloud, limit of one.
    refused = client.post("/instances", json=LAMBDA_TARGET)
    assert refused.status_code == 409, refused.text
    assert "Concurrency guard" in refused.json()["detail"]

    # A GCP launch is admitted, because GCP has nothing running.
    admitted = client.post("/instances", json={**GCP_TARGET,
                                               "provider": "gcp"})
    assert admitted.status_code == 202, admitted.text
    wait_for_launch_status(client, admitted.json()["launch"]["id"])
    assert len(gcp.instances) == 1


def test_pending_rows_count_across_every_cloud(client, db):
    """The other half of the asymmetry: a launch already ADMITTED but not
    yet visible on any cloud is Manifold's own commitment to spend, and it
    counts wherever it was made."""
    use_mock_gcp(client)
    db.create_launch(
        requested_type="g2-standard-4", region="us-central1", filesystem="",
        connection_mode="direct-ssh", hourly_rate_cents=70, provider="gcp",
    )

    resp = client.post("/instances", json=LAMBDA_TARGET)
    assert resp.status_code == 409, resp.text
    assert "Concurrency guard" in resp.json()["detail"]


# -- the bridge -----------------------------------------------------------------


@pytest.fixture
async def mcp_wired(tmp_path, mock_client, mock_storage, mock_sidecar):
    """The MCP bridge pointed at a real in-process app (see test_mcp.py)."""
    import httpx
    from asgi_lifespan import LifespanManager
    from app.main import create_app
    from tests.conftest import make_settings, mock_connect_fn

    app = create_app(
        make_settings(tmp_path),
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )
    async with LifespanManager(app) as manager:
        old = mcp_server._client
        mcp_server._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app),
            base_url="http://manifold.test",
        )
        yield app
        await mcp_server._client.aclose()
        mcp_server._client = old


async def test_mcp_launch_omits_provider_unless_asked(mcp_wired, mock_client):
    """The bridge sends no provider by default (so the account default
    decides) and passes one through verbatim when an agent overrides."""
    result = await mcp_server.launch_gpu(
        **LAMBDA_TARGET, purpose="provider-toggle test",
        note="default provider")
    assert "error" not in result, result
    assert result["launch"]["provider"] == "lambda"

    override = await mcp_server.launch_gpu(
        **GCP_TARGET, provider="gcp", purpose="provider-toggle test",
        note="explicit override")
    # GCP is unconfigured in this wiring, so the override is REFUSED - and
    # the refusal proves the name travelled (a dropped provider would have
    # launched on Lambda instead).
    assert "not configured yet" in override["error"], override
    assert len(mock_client.launch_calls) == 1


async def test_mcp_launch_options_name_their_cloud(mcp_wired):
    body = await mcp_server.list_launch_options(note="where can I launch")
    assert body["provider"] == "lambda"
    assert {t["provider"] for t in body["targets"]} == {"lambda"}

    gcp = await mcp_server.list_launch_options(provider="gcp",
                                               note="and on Google?")
    assert gcp["provider"] == "gcp"
    assert "not configured yet" in gcp["unavailable_reason"]


# -- the flip reaches a running backend with no restart -------------------------


def test_a_flip_takes_effect_on_the_very_next_launch(client, mock_client):
    """No restart, no re-threading: the preference is read at launch time,
    which is what lets one toggle reach agents already running."""
    gcp = use_mock_gcp(client)

    first = client.post("/instances", json=LAMBDA_TARGET)
    launch = wait_for_launch_status(client, first.json()["launch"]["id"])
    assert launch["provider"] == "lambda"

    set_default_provider(client, "gcp")
    second = client.post("/instances", json=GCP_TARGET)
    assert second.status_code == 202, second.text
    assert second.json()["launch"]["provider"] == "gcp"
    assert len(gcp.instances) == 1


def test_a_catalog_that_cannot_be_read_says_the_fix_not_http_500(client):
    """The launch form's FIRST call is the catalog, so this route decides
    what the whole panel says when Google cannot be reached.

    `_catalog_for` called the provider with no translation, so an expired
    ADC - the ordinary case, refresh tokens age out - left ProviderUnavailable
    to propagate as a bare 500, and the form rendered its entire body as the
    string "HTTP 500". The provider layer had already written the sentence
    that fixes it; the route threw it away. Reported by the owner from the
    front page on 2026-08-19 and reproduced against a credential-less
    backend.
    """
    client.app.state.orchestrator.providers.register("gcp",
                                                     _UnreadableCatalog())

    for route in ("/instance-types?provider=gcp", "/gpu-guide?provider=gcp"):
        resp = client.get(route)
        assert resp.status_code == 503, f"{route}: {resp.text}"
        # The one command that fixes it survives the trip intact.
        assert resp.json()["detail"] == _UnreadableCatalog.FIX

    # Lambda's catalog is untouched: one cloud being unreachable must not
    # take the other one's launch form down with it.
    assert client.get("/instance-types").status_code == 200
