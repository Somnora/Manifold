"""Integration tests for Elastic GPU Cluster Orchestration."""

import asyncio

import pytest
import httpx

from app.config import Guardrails
from app.db import Database, utcnow
from app.lambda_api import MockLambdaClient
from app.main import create_app
from app.orchestrator import LaunchPlan, LaunchRejected, Orchestrator
import app.mcp_server as mcp_server
from app.mcp_server import (
    launch_cluster as mcp_launch_cluster,
    list_clusters as mcp_list_clusters,
    get_cluster_details as mcp_get_cluster_details,
    terminate_cluster as mcp_terminate_cluster,
)
from tests.conftest import make_settings, mock_connect_fn


@pytest.fixture
def cluster_app(tmp_path, mock_client, mock_storage, mock_sidecar):
    """Real app with mock infrastructure for cluster testing.

    Clusters are guarded atomically (all N nodes must fit), so the default
    test guardrails (1 instance, $4/hr) would refuse a 2-node cluster;
    raise them to what a cluster legitimately needs."""
    settings = make_settings(tmp_path, guardrails=Guardrails(
        max_concurrent_instances=4, max_hourly_spend_usd=20.0))
    application = create_app(
        settings,
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )
    return application


@pytest.fixture
async def mcp_cluster_wired(cluster_app):
    """Point the MCP module at the cluster test app in-process."""
    from asgi_lifespan import LifespanManager
    async with LifespanManager(cluster_app) as manager:
        old = mcp_server._client
        mcp_server._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app),
            base_url="http://manifold.test",
        )
        yield manager.app
        await mcp_server._client.aclose()
        mcp_server._client = old


def test_db_cluster_methods(tmp_path):
    """Test database schema and query helpers for cluster tracking."""
    db_path = tmp_path / "test.db"
    db = Database(db_path)

    cluster_id = "cl_12345"
    db.create_cluster(
        cluster_id=cluster_id,
        name="test-ray-cluster",
        gpu_type="gpu_8x_h100_sxm5",
        region="us-east-1",
        filesystem="fs-test",
        node_count=2,
    )

    db.add_cluster_node(
        cluster_id=cluster_id,
        instance_id="inst_head",
        role="head",
        node_index=0,
        ip="10.0.0.1",
        status="running",
    )
    db.add_cluster_node(
        cluster_id=cluster_id,
        instance_id="inst_worker1",
        role="worker",
        node_index=1,
        ip="10.0.0.2",
        status="running",
    )

    db.update_cluster_status(
        cluster_id,
        status="active",
        head_instance_id="inst_head",
        head_ip="10.0.0.1",
    )

    cluster = db.get_cluster(cluster_id)
    assert cluster is not None
    assert cluster["id"] == cluster_id
    assert cluster["name"] == "test-ray-cluster"
    assert cluster["status"] == "active"
    assert cluster["head_instance_id"] == "inst_head"
    assert len(cluster["nodes"]) == 2
    assert cluster["nodes"][0]["role"] == "head"
    assert cluster["nodes"][1]["role"] == "worker"

    clusters = db.list_clusters()
    assert len(clusters) == 1
    assert clusters[0]["id"] == cluster_id


@pytest.mark.asyncio
async def test_cluster_api_routes_and_mcp_tools(mcp_cluster_wired):
    """Test REST API and MCP tools for cluster launch, list, details, and terminate."""
    # 1. Launch cluster via MCP tool
    res_launch = await mcp_launch_cluster(
        instance_type="gpu_1x_a10",
        region="us-east-1",
        filesystem="manifold-data",
        node_count=2,
        name="swarm-cluster",
    )
    assert "id" in res_launch
    cluster_id = res_launch["id"]
    assert res_launch["name"] == "swarm-cluster"
    assert res_launch["node_count"] == 2
    assert len(res_launch["nodes"]) == 2

    # 2. List clusters
    res_list = await mcp_list_clusters()
    assert len(res_list["clusters"]) >= 1
    assert any(c["id"] == cluster_id for c in res_list["clusters"])

    # 3. Get cluster details
    res_details = await mcp_get_cluster_details(cluster_id)
    assert res_details["id"] == cluster_id
    assert res_details["nodes"][0]["role"] == "head"

    # 4. Terminate cluster
    res_term = await mcp_terminate_cluster(cluster_id)
    assert res_term["cluster_id"] == cluster_id
    assert res_term["terminated"] is True


# -- guard + race coverage -----------------------------------------------------


def make_orchestrator(tmp_path, client, **setting_overrides):
    """Bare orchestrator over mocks, the same wiring conftest uses."""
    settings = make_settings(tmp_path, **setting_overrides)
    db = Database(settings.db_path)
    orch = Orchestrator(settings, client, db, connect_fn=mock_connect_fn)
    return orch, db


class StallingLambdaClient(MockLambdaClient):
    """launch_instance blocks forever — a launch frozen mid-flight, so a
    test can terminate a cluster while its nodes are still provisioning."""

    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()   # never set: launches hang

    async def launch_instance(self, **kwargs):
        await self.release.wait()
        return await super().launch_instance(**kwargs)


@pytest.mark.asyncio
async def test_cluster_over_concurrency_limit_rejected_atomically(
        tmp_path, mock_client):
    """4 nodes against a 1-instance limit: refused up front, with no ghost
    cluster row and no Lambda launch call."""
    orch, db = make_orchestrator(tmp_path, mock_client)   # limit 1, $4/hr
    with pytest.raises(LaunchRejected) as exc:
        await orch.launch_cluster(
            instance_type="gpu_1x_a10", region="us-east-1",
            filesystem="manifold-data", node_count=4)
    assert exc.value.reason_code == "concurrency"
    assert db.list_clusters() == []
    assert mock_client.launch_calls == []


@pytest.mark.asyncio
async def test_cluster_over_budget_rejected_atomically(tmp_path, mock_client):
    """4 x $1.29/hr = $5.16/hr against a $4/hr budget: refused as a whole
    even though each node fits individually."""
    orch, db = make_orchestrator(tmp_path, mock_client, guardrails=Guardrails(
        max_concurrent_instances=10, max_hourly_spend_usd=4.00))
    with pytest.raises(LaunchRejected) as exc:
        await orch.launch_cluster(
            instance_type="gpu_1x_a10", region="us-east-1",
            filesystem="manifold-data", node_count=4)
    assert exc.value.reason_code == "budget"
    assert db.list_clusters() == []
    assert mock_client.launch_calls == []


@pytest.mark.asyncio
async def test_budget_guard_counts_pending_launches(tmp_path, mock_client):
    """The two-quick-launch race on the BUDGET axis: two a10s already
    admitted (pending, not yet cloud-visible) at $1.29/hr each already
    exceed a $2.00 budget, so a 3rd single launch and a 2-node cluster must
    BOTH be refused on budget — not concurrency (the cap is deliberately
    high) — and neither may leave a row behind."""
    orch, db = make_orchestrator(tmp_path, mock_client, guardrails=Guardrails(
        max_concurrent_instances=10, max_hourly_spend_usd=2.00))
    for _ in range(2):
        db.create_launch(                       # pending: no instance id yet
            requested_type="gpu_1x_a10", region="us-east-1",
            filesystem="manifold-data", connection_mode="direct-ssh",
            hourly_rate_cents=129)
    assert db.pending_launch_spend_cents() == 258

    # A 3rd single launch.
    with pytest.raises(LaunchRejected) as exc:
        await orch.request_launch(
            instance_type="gpu_1x_a10", region="us-east-1",
            filesystem="manifold-data")
    assert exc.value.reason_code == "budget"

    # A 2-node cluster, judged the same way.
    with pytest.raises(LaunchRejected) as exc:
        await orch.launch_cluster(
            instance_type="gpu_1x_a10", region="us-east-1",
            filesystem="manifold-data", node_count=2)
    assert exc.value.reason_code == "budget"
    assert db.list_clusters() == []
    assert mock_client.launch_calls == []


@pytest.mark.asyncio
async def test_cluster_node_count_capped(tmp_path, mock_client):
    orch, db = make_orchestrator(tmp_path, mock_client)
    with pytest.raises(LaunchRejected) as exc:
        await orch.launch_cluster(
            instance_type="gpu_1x_a10", region="us-east-1",
            filesystem="manifold-data", node_count=17)
    assert exc.value.status_code == 400
    assert "16" in exc.value.detail
    assert db.list_clusters() == []


@pytest.mark.asyncio
async def test_terminate_provisioning_cluster_cancels_inflight_launches(
        tmp_path):
    """Terminating a cluster whose nodes have no instance yet must cancel
    their launch tasks — otherwise each task goes on to create a real
    billed instance whose row already says terminated."""
    client = StallingLambdaClient()
    orch, db = make_orchestrator(tmp_path, client, guardrails=Guardrails(
        max_concurrent_instances=8, max_hourly_spend_usd=50.0))
    cluster = await orch.launch_cluster(
        instance_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", node_count=2)
    cluster_id = cluster["id"]
    tasks = [orch._launch_tasks[n["instance_id"]] for n in cluster["nodes"]]
    assert all(not t.done() for t in tasks)

    res = await orch.terminate_cluster(cluster_id)
    assert res["terminated"] is True
    assert all(t.cancelled() for t in tasks)
    # The tasks died before their launch calls went through: nothing was
    # ever created on the cloud, and every node row is closed.
    assert client.instances == {}
    for node in cluster["nodes"]:
        assert db.get_launch(node["instance_id"])["status"] == "terminated"
    assert db.get_cluster(cluster_id)["status"] == "terminated"


@pytest.mark.asyncio
async def test_launch_aborts_when_row_already_terminated(
        tmp_path, mock_client):
    """Defense-in-depth for the terminate-while-launching race: a launch
    whose row was closed must abort instead of creating an instance."""
    orch, db = make_orchestrator(tmp_path, mock_client)
    launch_id = db.create_launch(
        requested_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=129)
    db.update_launch(launch_id, status="terminated", terminated_at=utcnow())
    plan = LaunchPlan(
        launch_id=launch_id, region="us-east-1", filesystem="manifold-data",
        connection_mode="direct-ssh", ssh_key_name="test-ssh-key",
        types_to_try=["gpu_1x_a10"], prices={"gpu_1x_a10": 129},
        name="race-victim")
    assert await orch._launch_with_retry(plan) is None
    assert mock_client.launch_calls == []
