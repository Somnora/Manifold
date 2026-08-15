"""Out-of-band termination: Lambda is the source of truth, Manifold follows.

James terminated an instance from outside Manifold and the dashboard kept
showing it. These tests pin the reconcile behavior that fixes that.

The second half pins what the sweep may and may not WRITE: an observed stop
gets terminated_at, an inferred one only ever gets resolved_at, and no row is
judged at all unless its own provider answered.
"""

import time

import pytest

from app.connections import ConnectionState
from app.orchestrator import Orchestrator
from app.providers.gcp_provider import MockGCPProvider, RealGCPProvider
from app.providers.lambda_provider import LambdaProvider
from app.providers.registry import ProviderRegistry
from tests.conftest import mock_connect_fn, wait_for_launch_status


def launch_connected(client, timeout=5.0):
    resp = client.post("/instances", json={
        "instance_type": "gpu_1x_a10", "region": "us-east-1",
        "filesystem": "manifold-data",
    })
    launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
    instance_id = launch["lambda_instance_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        inst = next(i for i in client.get("/instances").json()["instances"]
                    if i["id"] == instance_id)
        if inst["connection_state"] == "connected":
            return launch["id"], instance_id
        time.sleep(0.02)
    raise AssertionError("never connected")


def test_externally_terminated_instance_disappears(client, mock_client):
    launch_id, instance_id = launch_connected(client)

    # Terminate BEHIND Manifold's back (console/API), as James did. Lambda
    # keeps reporting the instance as 'terminated' for a while.
    mock_client.instances[instance_id].status = "terminated"

    # The very next poll drops the card...
    assert client.get("/instances").json()["instances"] == []

    # ...reaps the SSH supervisor (no reconnect-looping at a dead host)...
    orch = client.app.state.orchestrator
    assert instance_id not in orch.connections

    # ...and closes the history row so cost stops accruing.
    row = next(l for l in client.get("/launches").json()["launches"]
               if l["id"] == launch_id)
    assert row["status"] == "terminated"
    assert row["terminated_at"] is not None

    # The reconciliation is audited.
    audit = client.get("/audit").json()["entries"]
    assert any(e["action"] == "external_termination_detected" for e in audit)


def test_instance_deleted_entirely_from_lambda(client, mock_client):
    """Same, but the instance vanished from the list altogether."""
    launch_id, instance_id = launch_connected(client)
    del mock_client.instances[instance_id]

    assert client.get("/instances").json()["instances"] == []
    assert instance_id not in client.app.state.orchestrator.connections
    row = next(l for l in client.get("/launches").json()["launches"]
               if l["id"] == launch_id)
    assert row["status"] == "terminated"


def test_orphaned_active_row_closed_without_connection(client, mock_client, db):
    """A launch row left 'active' from a session where the instance was
    terminated while the backend was down still gets closed."""
    launch_id = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129,
    )
    db.update_launch(launch_id, status="active",
                     lambda_instance_id="i-long-gone")

    client.get("/instances")   # any poll reconciles
    row = next(l for l in client.get("/launches").json()["launches"]
               if l["id"] == launch_id)
    assert row["status"] == "terminated"


def test_healthy_instances_untouched_by_reconcile(client, mock_client):
    _, instance_id = launch_connected(client)
    # Repeated polls must not disturb a live instance or its connection.
    for _ in range(3):
        instances = client.get("/instances").json()["instances"]
    assert [i["id"] for i in instances] == [instance_id]
    conn = client.app.state.orchestrator.connections[instance_id]
    assert conn.state == ConnectionState.CONNECTED


# -- what the sweep may write (Phase 76) --------------------------------------

def _orchestrator(settings, db, mock_client, extra=()):
    """An orchestrator over the mock Lambda client plus any extra providers."""
    registry = ProviderRegistry()
    registry.register("lambda", LambdaProvider(mock_client))
    for name, provider in extra:
        registry.register(name, provider)
    return Orchestrator(settings, registry, db, connect_fn=mock_connect_fn)


async def _live_instance(mock_client, name="node-a", status="active"):
    instance_id = await mock_client.launch_instance(
        instance_type="gpu_1x_a10", region="us-east-1",
        ssh_key_names=["test-ssh-key"], filesystem_names=[], name=name)
    mock_client.instances[instance_id].status = status
    mock_client.instances[instance_id].ip = "10.0.0.5"
    return instance_id


def _seed(db, instance_id, status, *, provider="lambda", **fields):
    lid = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129, provider=provider)
    db.update_launch(lid, status=status, lambda_instance_id=instance_id,
                     **fields)
    return lid


@pytest.mark.asyncio
async def test_live_instances_are_stamped_with_last_seen_at(
        settings, db, mock_client):
    """Cheap evidence, gathered while the sweep is already iterating: without
    it, a launch that later stops unobserved has no defensible lower bound."""
    instance_id = await _live_instance(mock_client)
    lid = _seed(db, instance_id, "active", launched_at="2026-08-10T20:00:00+00:00")
    orch = _orchestrator(settings, db, mock_client)

    await orch.instances_with_state()

    assert db.get_launch(lid)["last_seen_at"] is not None
    await orch.shutdown()


@pytest.mark.asyncio
async def test_unobserved_stop_records_resolved_at_not_terminated_at(
        settings, db, mock_client):
    """A failed row whose instance is gone stopped at a time nobody watched.
    Recording a guessed terminated_at would turn that unknown into a lie."""
    lid = _seed(db, "i-long-gone", "failed",
                launched_at="2026-08-10T20:00:00+00:00")
    orch = _orchestrator(settings, db, mock_client)

    await orch.instances_with_state()

    row = db.get_launch(lid)
    assert row["terminated_at"] is None      # we never saw it stop
    assert row["resolved_at"] is not None    # but we know when we found out
    assert row["status"] == "failed"
    await orch.shutdown()


@pytest.mark.asyncio
async def test_observed_stop_stamps_both_columns(settings, db, mock_client):
    """An active row whose instance vanished IS an observed disappearance, so
    terminated_at stays legitimate — with resolved_at alongside it so the cost
    model can still tell the two apart."""
    lid = _seed(db, "i-vanished", "active",
                launched_at="2026-08-10T20:00:00+00:00")
    orch = _orchestrator(settings, db, mock_client)

    await orch.instances_with_state()

    row = db.get_launch(lid)
    assert row["status"] == "terminated"
    assert row["terminated_at"] is not None
    assert row["resolved_at"] is not None
    await orch.shutdown()


@pytest.mark.asyncio
async def test_orphaned_failed_row_is_repaired_when_its_instance_is_alive(
        settings, db, mock_client):
    """The boot-timeout case: the row gave up, the instance came up anyway and
    is billing. Repair the status, but never invent the boot timestamp."""
    instance_id = await _live_instance(mock_client)
    lid = _seed(db, instance_id, "failed", error="boot timed out",
                launched_at="2026-08-10T20:00:00+00:00")
    orch = _orchestrator(settings, db, mock_client)

    await orch.instances_with_state()

    row = db.get_launch(lid)
    assert row["status"] == "active"
    assert row["active_at"] is None      # we never saw this boot finish
    assert any(e["action"] == "orphan_repaired" for e in db.list_audit())
    await orch.shutdown()


@pytest.mark.asyncio
async def test_orphan_stays_failed_while_its_instance_is_still_booting(
        settings, db, mock_client):
    """Repairing a booting instance would re-enter the boot pipeline with no
    waiter behind it, so only a genuinely active instance repairs a row."""
    instance_id = await _live_instance(mock_client, status="booting")
    lid = _seed(db, instance_id, "failed",
                launched_at="2026-08-10T20:00:00+00:00")
    orch = _orchestrator(settings, db, mock_client)

    await orch.instances_with_state()

    assert db.get_launch(lid)["status"] == "failed"
    await orch.shutdown()


@pytest.mark.asyncio
async def test_rows_of_a_provider_that_did_not_list_are_left_alone(
        settings, db, mock_client):
    """Per-provider scoping. GCP cannot be read at all, so its rows are not
    evidence of anything — while Lambda's rows still reconcile normally."""
    gcp = RealGCPProvider(project_id="my-proj", default_zone="us-central1-a")
    lambda_row = _seed(db, "i-lambda-gone", "active",
                       launched_at="2026-08-10T20:00:00+00:00")
    gcp_row = _seed(db, "gcp-1", "active", provider="gcp",
                    launched_at="2026-08-10T20:00:00+00:00")
    orch = _orchestrator(settings, db, mock_client, extra=[("gcp", gcp)])

    await orch.instances_with_state()

    assert db.get_launch(lambda_row)["status"] == "terminated"
    still_open = db.get_launch(gcp_row)
    assert still_open["status"] == "active"
    assert still_open["resolved_at"] is None
    await orch.shutdown()


# -- reaping an adopted connection (Phase 76) ---------------------------------


class _OutageProvider(MockGCPProvider):
    """A provider that works in general and just failed THIS call. It may well
    be running the instance we are about to write off, so it must still block
    conclusions — unlike one that cannot be read at all."""

    async def list_instances(self, *, fresh: bool = False):
        raise RuntimeError("provider returned 503")


def _adopt(orch, mock_client, instance_id):
    """A managed connection with no launch row behind it: what adoption leaves
    for an instance Manifold did not launch."""
    orch._open_connection("direct-ssh", mock_client.instances[instance_id])
    assert instance_id in orch.connections


@pytest.mark.asyncio
async def test_adopted_connection_is_reaped_when_a_provider_is_unavailable(
        settings, db, mock_client):
    """A provider that cannot be READ owns nothing: no launch of ours ever
    reached it, so it has no answer to owe and must not veto conclusions.

    The regression this pins: GCP is always registered, and setting
    GCP_PROJECT_ID from the Settings page makes it raise ProviderUnavailable.
    Counting that as "has not answered yet" silently disabled reaping for
    every adopted connection, leaving supervisors reconnect-looping at dead
    hosts forever."""
    instance_id = await _live_instance(mock_client)

    class _UnavailableProvider(RealGCPProvider):
        """The contract under test, without the network.

        This used to construct a real RealGCPProvider with a fake project
        and rely on the STUB raising ProviderUnavailable. Once Phase 87
        made the provider real, the test's verdict depended on the
        developer's gcloud state: expired ADC raised the mapped
        ProviderUnavailable (pass), a VALID login reached Google's live
        API and got a 403 for the fake project (fail). A test whose result
        changes when the developer signs into gcloud is reaching the real
        network, which the mocks-only rule exists to prevent.
        """

        async def list_instances(self, *, fresh: bool = False):
            from app.providers.base import ProviderUnavailable
            raise ProviderUnavailable("this provider cannot be read")

    gcp = _UnavailableProvider(project_id="my-proj",
                               default_zone="us-central1-a")
    orch = _orchestrator(settings, db, mock_client, extra=[("gcp", gcp)])
    _adopt(orch, mock_client, instance_id)

    del mock_client.instances[instance_id]        # terminated out of band
    await orch.instances_with_state()

    assert instance_id not in orch.connections
    # An adopted instance has no launch row to close, so the audit row is the
    # only trace its disappearance leaves.
    assert any(entry["action"] == "external_termination_detected"
               for entry in db.list_audit())
    await orch.shutdown()


@pytest.mark.asyncio
async def test_adopted_connection_survives_a_transient_provider_failure(
        settings, db, mock_client):
    """The other half of the same distinction: a provider that broke on this
    call might be the one running this instance, so an incomplete snapshot
    must not reap a healthy connection."""
    instance_id = await _live_instance(mock_client)
    orch = _orchestrator(settings, db, mock_client,
                         extra=[("gcp", _OutageProvider())])
    _adopt(orch, mock_client, instance_id)

    del mock_client.instances[instance_id]
    await orch.instances_with_state()

    assert instance_id in orch.connections
    assert not any(entry["action"] == "external_termination_detected"
                   for entry in db.list_audit())
    await orch.shutdown()
