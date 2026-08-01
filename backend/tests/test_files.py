"""File bridge: upload/download over the managed SSH connection (SFTP)."""

import io

import httpx
import pytest
from fastapi.testclient import TestClient

import app.mcp_server as mcp_server
from tests.test_reconcile import launch_connected


def sftp_store(client, instance_id) -> dict:
    """The mock SFTP in-memory filesystem behind the managed connection."""
    conn = client.app.state.orchestrator.connections[instance_id]
    return conn.ssh_connection().sftp_files


def test_upload_relative_lands_on_persistent(client):
    _, instance_id = launch_connected(client)
    resp = client.post(
        f"/instances/{instance_id}/files/upload",
        files={"file": ("sprite.png", io.BytesIO(b"PNG-bytes"), "image/png")},
        data={"dest": "inbox/"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"path": "/lambda/nfs/manifold-data/inbox/sprite.png",
                    "bytes": 9}
    assert sftp_store(client, instance_id)[body["path"]] == b"PNG-bytes"

    # Audited.
    audit = client.get("/audit").json()["entries"]
    assert any(e["action"] == "file_upload" for e in audit)


def test_upload_explicit_filename_and_absolute_path(client):
    _, instance_id = launch_connected(client)
    resp = client.post(
        f"/instances/{instance_id}/files/upload",
        files={"file": ("x.bin", io.BytesIO(b"abc"))},
        data={"dest": "/workspace/ephemeral/scratch/renamed.bin"},
    )
    assert resp.status_code == 200
    assert resp.json()["path"] == "/workspace/ephemeral/scratch/renamed.bin"


def test_upload_rejects_traversal_and_foreign_roots(client):
    _, instance_id = launch_connected(client)
    for dest in ("../../etc/cron.d/evil", "/etc/passwd",
                 "/lambda/nfs/../../root/x", "/home/ubuntu/.ssh/keys"):
        resp = client.post(
            f"/instances/{instance_id}/files/upload",
            files={"file": ("evil", io.BytesIO(b"x"))},
            data={"dest": dest},
        )
        assert resp.status_code == 400, dest
        assert "must stay under" in resp.json()["detail"]
    assert all("evil" not in p and "passwd" not in p
               for p in sftp_store(client, instance_id))


def test_download_roundtrip(client):
    _, instance_id = launch_connected(client)
    store = sftp_store(client, instance_id)
    store["/lambda/nfs/manifold-data/outputs/model.glb"] = b"GLB" * 1000

    resp = client.get(
        f"/instances/{instance_id}/files/download",
        params={"path": "outputs/model.glb"},
    )
    assert resp.status_code == 200
    assert resp.content == b"GLB" * 1000
    assert 'filename="model.glb"' in resp.headers["content-disposition"]


def test_download_missing_file_is_404(client):
    _, instance_id = launch_connected(client)
    resp = client.get(
        f"/instances/{instance_id}/files/download",
        params={"path": "outputs/nope.glb"},
    )
    assert resp.status_code == 404


def test_transfer_requires_connected_instance(client):
    resp = client.post(
        "/instances/i-none/files/upload",
        files={"file": ("x", io.BytesIO(b"x"))},
        data={"dest": "inbox/"},
    )
    assert resp.status_code == 409
    resp = client.get("/instances/i-none/files/download",
                      params={"path": "outputs/a.txt"})
    assert resp.status_code == 409


# -- MCP tools --------------------------------------------------------------------


@pytest.fixture
async def mcp_wired_client(tmp_path, mock_client, mock_storage, mock_sidecar):
    """Real app + MCP module pointed at it in-process (same as test_mcp)."""
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


async def _launch_connected_async(app, mock_client):
    import asyncio
    from app.connections import ConnectionState
    orch = app.state.orchestrator
    launch = await orch.request_launch(
        instance_type="gpu_1x_a10", region="us-east-1",
        filesystem="manifold-data",
    )
    final = await orch.wait_for_launch(launch["id"])
    instance_id = final["lambda_instance_id"]
    for _ in range(200):
        conn = orch.connections.get(instance_id)
        if conn and conn.state == ConnectionState.CONNECTED:
            return instance_id
        await asyncio.sleep(0.01)
    raise AssertionError("never connected")


async def test_mcp_upload_download_roundtrip(mcp_wired_client, mock_client,
                                             tmp_path):
    app = mcp_wired_client
    instance_id = await _launch_connected_async(app, mock_client)

    # Agent uploads a local sprite (instance auto-selected: only one).
    local = tmp_path / "sprite.png"
    local.write_bytes(b"sprite-data")
    result = await mcp_server.upload_file(str(local), "inbox/",
                                          note="asset for 3d gen")
    assert result == {"path": "/lambda/nfs/manifold-data/inbox/sprite.png",
                      "bytes": 11}

    # ...a job would produce an output; simulate it, then download it back.
    conn = app.state.orchestrator.connections[instance_id]
    conn.ssh_connection().sftp_files[
        "/lambda/nfs/manifold-data/outputs/sprite.glb"] = b"mesh-bytes"
    out = tmp_path / "downloads" / "sprite.glb"
    result = await mcp_server.download_file("outputs/sprite.glb", str(out),
                                            note="fetch the mesh")
    assert result == {"local_path": str(out), "bytes": 10, "complete": True}
    assert out.read_bytes() == b"mesh-bytes"


async def test_mcp_upload_missing_local_file(mcp_wired_client):
    result = await mcp_server.upload_file("/no/such/file.png")
    assert "local file not found" in result["error"]


# -- persistent-file browse when the S3 "Files" keys are absent -------------------
#
# Field case (2026-07-15, real A10): list_persistent_files failed with a
# cryptic "Expecting value: line 1 column 1 (char 0)". Root cause: the S3
# "Files" API keys were empty in .env, so the storage factory raised; the
# route let that become a 500 plain-text page, and the MCP _call helper
# crashed decoding that page as JSON. Both layers are hardened here.

def _raises_missing_creds(fs):
    raise ValueError("S3 adapter credentials are not configured in .env")


def test_storage_browse_without_s3_credentials_is_clean_503(
        tmp_path, mock_client, mock_sidecar):
    """Empty S3 keys yield an actionable 503, not an opaque 500 whose
    plain-text body decodes to nothing downstream."""
    from app.main import create_app
    from tests.conftest import make_settings, mock_connect_fn
    app = create_app(
        make_settings(tmp_path),
        lambda_client=mock_client,
        storage_factory=_raises_missing_creds,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )
    with TestClient(app) as c:
        resp = c.get("/storage/files",
                     params={"filesystem": "manifold-data", "prefix": ""})
    assert resp.status_code == 503
    assert "credentials" in resp.json()["detail"]


@pytest.fixture
async def mcp_client_no_s3(tmp_path, mock_client, mock_sidecar):
    """MCP module wired to an app whose storage factory raises (no S3 keys)."""
    from asgi_lifespan import LifespanManager
    from app.main import create_app
    from tests.conftest import make_settings, mock_connect_fn
    app = create_app(
        make_settings(tmp_path),
        lambda_client=mock_client,
        storage_factory=_raises_missing_creds,
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


async def test_mcp_list_persistent_files_surfaces_clean_error(mcp_client_no_s3):
    """No connected instance AND no S3 keys: the agent sees the real reason
    (plus a pointer to the keyless route), not a JSON-decode crash."""
    result = await mcp_server.list_persistent_files(filesystem="manifold-data")
    assert "error" in result
    assert "credentials" in result["error"]
    # The backend answered (a 503) - this is a rejection, not unreachability.
    assert not result.get("unreachable")
    # And it tells the agent how to browse without keys.
    assert "SSH" in result.get("hint", "")


async def test_mcp_list_persistent_files_browses_over_ssh_without_s3(
        mcp_wired_client, mock_client):
    """With a box up that mounts the filesystem, browsing rides the SSH
    connection (the sidecar) and needs no S3 keys — the connected instance is
    preferred even though S3 is configured here."""
    app = mcp_wired_client
    await _launch_connected_async(app, mock_client)

    # Top level of the mounted filesystem: served from the sidecar tree.
    result = await mcp_server.list_persistent_files(note="browse persistent")
    assert "error" not in result
    assert "ssh" in result["source"]                 # keyless instance path
    assert result["filesystem"] == "manifold-data"
    names = [e["name"] for e in result["entries"]]
    assert "outputs" in names and "datasets" in names
    assert "files" not in result                     # NOT the S3 shape

    # `prefix` is filesystem-relative, exactly like the S3 path.
    sub = await mcp_server.list_persistent_files(prefix="outputs")
    assert "error" not in sub
    assert [e["name"] for e in sub["entries"]] == ["transcripts"]


async def test_call_degrades_on_non_json_error_body():
    """A 500 that escapes a route is a Starlette plain-text page; _call must
    surface it as a clean error instead of crashing on the JSON decode."""
    async def plain_500(scope, receive, send):
        # Audit posts must not interfere; answer them with valid JSON.
        if scope["path"] == "/audit/agent":
            status, body = 200, b"{}"
        else:
            status, body = 500, b"Internal Server Error"
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": body})

    old = mcp_server._client
    mcp_server._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=plain_500),
        base_url="http://manifold.test",
    )
    try:
        result = await mcp_server._call("probe", "GET", "/x", note="")
    finally:
        await mcp_server._client.aclose()
        mcp_server._client = old
    assert "error" in result
    assert "Internal Server Error" in result["error"]
    assert not result.get("unreachable")


def test_mcp_server_still_structurally_thin():
    """The new tools must not have widened the import allowlist."""
    import ast
    from pathlib import Path
    tree = ast.parse(Path(mcp_server.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))
    # asyncio: stdlib retry plumbing for wait_for_launch, not backend access.
    assert imported <= {"__future__", "asyncio", "json", "os", "typing", "httpx",
                        "mcp.server.fastmcp"}

# -- ranged download + resumable MCP download (Game Admin report 2026-07-17) ------


def test_download_range_slices_and_reports_size(client):
    _, instance_id = launch_connected(client)
    store = sftp_store(client, instance_id)
    store["/lambda/nfs/manifold-data/outputs/big.bin"] = b"0123456789"

    resp = client.get(
        f"/instances/{instance_id}/files/download",
        params={"path": "outputs/big.bin", "offset": 3, "max_bytes": 4},
    )
    assert resp.status_code == 200
    assert resp.content == b"3456"
    assert resp.headers["x-file-size"] == "10"
    assert resp.headers["x-offset"] == "3"

    # An offset past the end means the remote file changed: explicit 416.
    resp = client.get(
        f"/instances/{instance_id}/files/download",
        params={"path": "outputs/big.bin", "offset": 11},
    )
    assert resp.status_code == 416


async def test_mcp_download_resumes_across_calls(mcp_wired_client, mock_client,
                                                 tmp_path, monkeypatch):
    """A big file with a tiny time budget: the first call returns partial
    progress instead of timing out, the second call resumes the .part and
    completes. This is the incident fix - a 127MB output needed manual
    split/reassemble before."""
    app = mcp_wired_client
    instance_id = await _launch_connected_async(app, mock_client)
    conn = app.state.orchestrator.connections[instance_id]
    data = bytes(range(256)) * 40                      # 10240 bytes
    conn.ssh_connection().sftp_files[
        "/lambda/nfs/manifold-data/outputs/big.glb"] = data

    monkeypatch.setattr(mcp_server, "DOWNLOAD_CHUNK_BYTES", 4096)
    monkeypatch.setattr(mcp_server, "DOWNLOAD_TIME_BUDGET_SECONDS", 0.0)
    out = tmp_path / "big.glb"

    first = await mcp_server.download_file("outputs/big.glb", str(out))
    assert first["complete"] is False
    assert first["bytes_done"] == 4096
    assert first["total_bytes"] == len(data)
    assert (tmp_path / "big.glb.part").exists()
    assert not out.exists()

    # Same arguments again: resumes, and with a real budget it finishes.
    monkeypatch.setattr(mcp_server, "DOWNLOAD_TIME_BUDGET_SECONDS", 40.0)
    second = await mcp_server.download_file("outputs/big.glb", str(out))
    assert second == {"local_path": str(out), "bytes": len(data),
                      "complete": True}
    assert out.read_bytes() == data
    assert not (tmp_path / "big.glb.part").exists()   # cleaned up


async def test_mcp_download_restarts_when_remote_changed(
        mcp_wired_client, mock_client, tmp_path):
    """A stale .part larger than the remote file (file was regenerated
    smaller) must not poison the download: the tool restarts from zero."""
    app = mcp_wired_client
    instance_id = await _launch_connected_async(app, mock_client)
    conn = app.state.orchestrator.connections[instance_id]
    conn.ssh_connection().sftp_files[
        "/lambda/nfs/manifold-data/outputs/o.bin"] = b"fresh"

    out = tmp_path / "o.bin"
    (tmp_path / "o.bin.part").write_bytes(b"x" * 100)  # stale, too big

    result = await mcp_server.download_file("outputs/o.bin", str(out))
    assert result["complete"] is True
    assert out.read_bytes() == b"fresh"
