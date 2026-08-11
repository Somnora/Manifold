import pytest
import sqlite3
from app.providers.gcp_provider import MockGCPProvider, RealGCPProvider
from app.providers.registry import ProviderRegistry
from app.providers.lambda_provider import LambdaProvider
from app.providers.base import ProviderError
from app.db import Database, SCHEMA
from app.lambda_api import MockLambdaClient
from app.orchestrator import Orchestrator
from tests.conftest import make_settings, mock_connect_fn

@pytest.mark.asyncio
async def test_mock_gcp_provider():
    provider = MockGCPProvider()
    
    # Test catalog
    types = await provider.list_instance_types()
    assert len(types) == 4
    type_names = [t.name for t in types]
    assert "g2-standard-4" in type_names
    assert "a2-highgpu-1g" in type_names

    # Test launch
    instance_id = await provider.launch_instance(
        region="us-central1",
        instance_type="g2-standard-4",
        ssh_key_names=["test-key"],
        filesystem_names=[],
        name="test-instance"
    )
    assert instance_id is not None

    # Test list
    instances = await provider.list_instances()
    assert len(instances) == 1
    assert instances[0].id == instance_id
    assert instances[0].status == "active"
    assert instances[0].provider == "gcp"

    # Test get
    inst = await provider.get_instance(instance_id)
    assert inst is not None
    assert inst.name == "test-instance"

    # Test terminate
    success = await provider.terminate_instance(instance_id)
    assert success is True
    instances = await provider.list_instances()
    assert len(instances) == 0

@pytest.mark.asyncio
async def test_real_gcp_provider_degrades_instead_of_raising():
    """Setting GCP_PROJECT_ID before the live API exists must not brick the
    read paths: list/get DEGRADE to empty. Only the explicit write actions
    raise, and they raise a typed, catchable ProviderError — never a bare
    NotImplementedError that could escape a background sweep."""
    gcp = RealGCPProvider(project_id="my-proj", default_zone="us-central1-a")

    assert await gcp.list_instances() == []
    assert await gcp.list_instances(fresh=True) == []
    assert await gcp.list_instance_types() == []
    assert await gcp.get_instance("whatever") is None

    with pytest.raises(ProviderError):
        await gcp.launch_instance("us-central1", "g2-standard-4", ["k"], [])
    with pytest.raises(ProviderError):
        await gcp.terminate_instance("whatever")
    with pytest.raises(ProviderError):
        await gcp.ensure_ssh_key("ssh-rsa AAA", "key")


class _BoomProvider(MockGCPProvider):
    """A provider whose list_instances always raises — stands in for a broken
    or misconfigured provider, to prove one bad provider can't blank the view."""
    async def list_instances(self, *, fresh: bool = False):
        raise RuntimeError("provider on fire")


@pytest.mark.asyncio
async def test_instances_view_and_adoption_survive_failing_providers(tmp_path):
    """A not-yet-implemented (RealGCPProvider) or outright failing provider
    must DEGRADE: instances_with_state() still returns Lambda's instances and
    does not raise, and adoption still runs for Lambda."""
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)

    lambda_client = MockLambdaClient()
    iid = await lambda_client.launch_instance(
        instance_type="gpu_1x_a10", region="us-east-1",
        ssh_key_names=["test-ssh-key"], filesystem_names=[], name="node-a")
    lambda_client.instances[iid].status = "active"
    lambda_client.instances[iid].ip = "10.0.0.5"

    reg = ProviderRegistry()
    reg.register("lambda", LambdaProvider(lambda_client))
    # project_id set, but the real GCP API is not wired up yet.
    reg.register("gcp", RealGCPProvider(
        project_id="my-proj", default_zone="us-central1-a"))
    # A provider that outright explodes, to prove per-provider isolation.
    reg.register("boom", _BoomProvider())

    orch = Orchestrator(settings, reg, db, connect_fn=mock_connect_fn)

    cards = await orch.instances_with_state()
    assert [c["id"] for c in cards] == [iid]   # Lambda survived the others

    adopted = await orch.adopt_running_instances(startup=True)
    assert adopted == 1                        # adoption not disabled

    await orch.shutdown()
    db.close()


def test_provider_registry():
    registry = ProviderRegistry()
    gcp = MockGCPProvider()
    
    # Lambda provider is missing here, but we can register anything
    registry.register("gcp", gcp)
    
    assert registry.get_provider("gcp") is gcp
    with pytest.raises(ValueError):
        registry.get_provider("lambda")
    
    registry.set_active_provider("gcp")
    assert registry.active_provider is gcp

def test_db_migration(tmp_path):
    db_path = str(tmp_path / "test.db")
    
    # Create DB without provider column
    conn = sqlite3.connect(db_path)
    # create table without provider
    conn.execute("""
        CREATE TABLE launches (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            requested_type TEXT NOT NULL,
            region TEXT NOT NULL,
            filesystem TEXT,
            connection_mode TEXT NOT NULL,
            hourly_rate_cents INTEGER,
            idle_timeout_seconds INTEGER,
            status TEXT NOT NULL
        )
    """)
    conn.execute("INSERT INTO launches (id, created_at, requested_type, region, connection_mode, status) VALUES ('123', '2026-07-25', 'g2', 'us-central1', 'ssh', 'launching')")
    conn.commit()
    conn.close()

    # Now init Database, which should apply the migration
    db = Database(db_path)
    launch = db.get_launch('123')
    assert launch is not None
    assert launch['provider'] == 'lambda' # Default value
    
    # Test create_launch with GCP provider
    launch_id = db.create_launch(
        requested_type="g2-standard-4",
        region="us-central1",
        filesystem=None,
        connection_mode="ssh",
        hourly_rate_cents=70,
        provider="gcp"
    )
    
    new_launch = db.get_launch(launch_id)
    assert new_launch["provider"] == "gcp"
