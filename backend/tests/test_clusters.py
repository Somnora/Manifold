"""Integration tests for Elastic GPU Cluster Orchestration."""

import pytest
import httpx

from app.db import Database
from app.main import create_app
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
    """Real app with mock infrastructure for cluster testing."""
    settings = make_settings(tmp_path)
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
