import pytest
import sqlite3
from app.providers.gcp_provider import MockGCPProvider
from app.providers.registry import ProviderRegistry
from app.providers.lambda_provider import LambdaProvider
from app.db import Database, SCHEMA

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
