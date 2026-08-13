import os
import pytest
from unittest.mock import patch, mock_open

from app.ide_attach import (
    write_ssh_config_block,
    remove_ssh_config_block,
    get_ide_urls,
    SSH_CONFIG_PATH
)
from app.dispatcher import Dispatcher

@pytest.fixture
def mock_ssh_config(tmp_path):
    config_file = tmp_path / "config"
    with patch("app.ide_attach.SSH_CONFIG_PATH", str(config_file)):
        yield config_file

def test_get_ide_urls():
    urls = get_ide_urls("inst-123")
    assert urls["vscode_url"] == "vscode://vscode-remote/ssh-remote+manifold-inst-123/workspace/ephemeral"
    assert urls["cursor_url"] == "cursor://vscode-remote/ssh-remote+manifold-inst-123/workspace/ephemeral"
    assert urls["ssh_alias"] == "manifold-inst-123"
    assert urls["ssh_command"] == "ssh manifold-inst-123"

def test_write_ssh_config_block(mock_ssh_config):
    # Test creation
    write_ssh_config_block("inst-1", "1.2.3.4", "/key", "/known_hosts")
    
    assert mock_ssh_config.exists()
    content = mock_ssh_config.read_text()
    assert "Host manifold-inst-1" in content
    assert "HostName 1.2.3.4" in content
    
    # Add custom user content
    mock_ssh_config.write_text("Host custom\n  HostName 8.8.8.8\n" + content)
    
    # Test idempotency and updates
    write_ssh_config_block("inst-1", "5.6.7.8", "/key2", "/known_hosts2")
    
    content2 = mock_ssh_config.read_text()
    assert "Host custom\n  HostName 8.8.8.8" in content2
    assert "HostName 5.6.7.8" in content2
    assert "HostName 1.2.3.4" not in content2
    assert content2.count("Host manifold-inst-1") == 1

def test_remove_ssh_config_block(mock_ssh_config):
    mock_ssh_config.write_text("Host custom\n  HostName 8.8.8.8\n")
    write_ssh_config_block("inst-1", "1.2.3.4", "/key", "/known_hosts")
    
    content = mock_ssh_config.read_text()
    assert "Host manifold-inst-1" in content
    
    remove_ssh_config_block("inst-1")
    
    content2 = mock_ssh_config.read_text()
    assert "Host manifold-inst-1" not in content2
    assert "Host custom" in content2

def test_remove_ssh_config_block_nonexistent(mock_ssh_config):
    # Should not raise
    remove_ssh_config_block("inst-1")
    assert not mock_ssh_config.exists()

from unittest.mock import MagicMock, AsyncMock

@pytest.mark.asyncio
async def test_active_ide_session_prevents_idle():
    # Mock sidecar metrics to return active_ide_processes
    mock_sidecar = MagicMock()
    mock_sidecar.metrics = AsyncMock(return_value={"available": True, "gpus": [{"name": "A100", "vram_used_mib": 0, "vram_total_mib": 40000, "utilization_pct": 0}], "active_ide_processes": ["vscode"]})
    
    mock_orchestrator = MagicMock()
    mock_orchestrator.sidecar_for.return_value = mock_sidecar
    mock_orchestrator.gpu_metrics_via_ssh = AsyncMock(return_value=None)
    mock_conn = MagicMock()
    # Dispatcher connection state check uses `conn.state != ConnectionState.CONNECTED`, we should mock the enum
    from app.connections import ConnectionState
    mock_conn.state = ConnectionState.CONNECTED
    mock_orchestrator.connections = {"inst-1": mock_conn}
    
    dispatcher = Dispatcher(
        settings=MagicMock(),
        orchestrator=mock_orchestrator,
        queue=MagicMock(),
        templates={},
        db=MagicMock(),
        lambda_client=MagicMock()
    )
    dispatcher.touch_activity = MagicMock()
    
    await dispatcher._sample_telemetry_once()
    
    dispatcher.touch_activity.assert_called_once_with("inst-1")

def test_attach_on_non_active_instance(tmp_path):
    # Test attach_ide in main.py via TestClient. Harness wiring, NOT
    # create_default_app: since Phase 78 a production-wired app generates
    # an API token into the real DATA_ROOT/.env on construction, and a
    # test must never write the developer's .env (this one did, once).
    from fastapi.testclient import TestClient
    from app.lambda_api import MockLambdaClient
    from app.main import create_app
    from tests.conftest import make_settings, mock_connect_fn
    app = create_app(
        make_settings(tmp_path),
        lambda_client=MockLambdaClient(),
        connect_fn=mock_connect_fn,
        env_path=tmp_path / ".env",
        custom_templates_dir=tmp_path / "custom-templates",
    )
    client = TestClient(app)

    res = client.post("/instances/inst-unknown/ide-attach")
    assert res.status_code == 409
    assert "no connected instance" in res.text


def test_attach_happy_path_writes_config(tmp_path):
    """A connected instance -> 200 + a well-formed config block. The config is
    written to a patched temp path (NEVER the developer's real ~/.ssh/config),
    and the block must carry the real configured SSH key + host-keys paths."""
    import os
    import time
    from fastapi.testclient import TestClient
    from app.config import IdleSettings, TaskSettings, WatchSettings
    from app.image_checker import MockImageChecker
    from app.main import create_app
    from tests.conftest import (make_settings, mock_connect_fn,
                                wait_for_launch_status)

    settings = make_settings(
        tmp_path,
        tasks=TaskSettings(poll_seconds=0.02),
        idle=IdleSettings(timeout_seconds=60, poll_seconds=10),
        watches=WatchSettings(poll_seconds=60),
    )
    from app.lambda_api import MockLambdaClient
    from app.storage import MockStorage
    from app.sidecar_client import MockSidecarClient
    app = create_app(
        settings,
        lambda_client=MockLambdaClient(),
        storage_factory=lambda fs: MockStorage(),
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: MockSidecarClient(),
        image_checker=MockImageChecker(),
    )

    config_file = tmp_path / "ssh_config"
    with patch("app.ide_attach.SSH_CONFIG_PATH", str(config_file)):
        with TestClient(app) as client:
            resp = client.post("/instances", json={
                "instance_type": "gpu_1x_a10",
                "region": "us-east-1",
                "filesystem": "manifold-data",
            })
            assert resp.status_code == 202
            launch = wait_for_launch_status(client, resp.json()["launch"]["id"])
            instance_id = launch["lambda_instance_id"]

            # Wait for the managed SSH connection to come up.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                inst = next(i for i in client.get("/instances").json()["instances"]
                            if i["id"] == instance_id)
                if inst["connection_state"] == "connected":
                    break
                time.sleep(0.02)

            res = client.post(f"/instances/{instance_id}/ide-attach")
            assert res.status_code == 200, res.text
            urls = res.json()
            assert urls["ssh_alias"] == f"manifold-{instance_id}"

    content = config_file.read_text()
    assert f"Host manifold-{instance_id}" in content
    assert "HostName " in content
    # The block references the REAL configured key and the host-keys store,
    # not the bogus settings.ssh_key_path/host_keys_path the route used before.
    expected_key = os.path.expanduser(settings.ssh.private_key_path)
    assert f"IdentityFile {expected_key}" in content
    assert "host_keys.json" in content

