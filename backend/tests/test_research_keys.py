"""Phase 100: the research-key vault.

The owner's problem: research API keys (YouTube, X, the congress.gov keys
behind Tally) scattered across every agent CLI's own dotfolder, re-plumbed
for every new agent, hunted down on every rotation. The vault is one
audited place behind the guarded backend: deposit once, every agent
inherits it at connect time.

The security spine these tests pin:
- values live in ONE file and never appear in a list response, an audit
  row, or SQLite;
- Manifold's OWN credentials live in a DIFFERENT file the handout
  endpoint structurally cannot read;
- every handout carries a required purpose and lands in the audit log;
- reading values is operator-tier, listing (presence/length) is viewer.
"""

import os

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.research_keys import ResearchKeyStore
from tests.conftest import make_settings, mock_connect_fn

SECRET = "sk-research-4f9d8e7c6b5a-EXAMPLE"


def put_key(client, name="congress_gov", value=SECRET,
            note="legislation research"):
    resp = client.put(f"/research-keys/{name}",
                      json={"value": value, "note": note})
    assert resp.status_code == 200, resp.text
    return resp.json()


def fetch(client, name, purpose="pulling bill metadata"):
    return client.post(f"/research-keys/{name}/value",
                       json={"purpose": purpose})


# -- the vault holds and hands out -------------------------------------------


def test_set_then_list_shows_presence_never_value(client):
    entry = put_key(client)
    assert entry["present"] is True
    assert entry["length"] == len(SECRET)
    resp = client.get("/research-keys")
    assert resp.status_code == 200
    keys = resp.json()["keys"]
    assert [k["name"] for k in keys] == ["congress_gov"]
    assert keys[0]["note"] == "legislation research"
    # The strongest form of "never values": the secret's bytes do not
    # appear ANYWHERE in the list response.
    assert SECRET not in resp.text


def test_fetch_returns_value_and_stamps_use(client):
    put_key(client)
    resp = fetch(client, "congress_gov")
    assert resp.status_code == 200
    assert resp.json() == {"name": "congress_gov", "value": SECRET}
    row = client.get("/research-keys").json()["keys"][0]
    assert row["last_used_at"] is not None
    assert row["last_used_by"] == "api"   # the open backend's principal


def test_fetch_requires_a_purpose(client):
    put_key(client)
    assert client.post("/research-keys/congress_gov/value",
                       json={}).status_code == 422
    assert client.post("/research-keys/congress_gov/value",
                       json={"purpose": ""}).status_code == 422


def test_unknown_key_404_names_what_exists(client):
    put_key(client, name="youtube", value="yt-key-123")
    resp = fetch(client, "x_api")
    assert resp.status_code == 404
    assert "youtube" in resp.json()["detail"]   # the agent recovers in one step


def test_rotate_overwrites_in_place(client):
    put_key(client, value="old-value-1")
    entry = put_key(client, value="new-value-22")
    assert entry["length"] == len("new-value-22")
    assert fetch(client, "congress_gov").json()["value"] == "new-value-22"
    assert len(client.get("/research-keys").json()["keys"]) == 1


def test_delete_removes_for_everyone(client):
    put_key(client)
    assert client.delete("/research-keys/congress_gov").status_code == 200
    assert client.get("/research-keys").json()["keys"] == []
    assert client.delete("/research-keys/congress_gov").status_code == 404
    assert fetch(client, "congress_gov").status_code == 404


# -- the file -----------------------------------------------------------------


def test_vault_file_is_owner_only(client):
    put_key(client)
    store: ResearchKeyStore = client.app.state.research_keys
    assert oct(os.stat(store.path).st_mode & 0o777) == "0o600"


def test_hand_edited_key_is_honored(client):
    """The file is a supported client: a human with a text editor."""
    store: ResearchKeyStore = client.app.state.research_keys
    put_key(client)                      # ensures the file exists
    with open(store.path, "a", encoding="utf-8") as fh:
        fh.write("# added by hand\nhand_added=manual-key-77\n")
    rows = {k["name"]: k for k in client.get("/research-keys").json()["keys"]}
    assert rows["hand_added"]["present"] is True
    assert rows["hand_added"]["created_by"] is None   # provenance unknown, said so
    assert fetch(client, "hand_added").json()["value"] == "manual-key-77"
    put_key(client, value="rotated-1")   # a rewrite must not eat the hand edits
    assert "# added by hand" in store.path.read_text()


def test_meta_without_value_says_present_false(client):
    """A hand-deleted value leaves annotation behind: shown as absent,
    never silently dropped and never length 0."""
    put_key(client)
    store: ResearchKeyStore = client.app.state.research_keys
    store.delete("congress_gov")         # file-level removal, meta row stays
    row = client.get("/research-keys").json()["keys"][0]
    assert row["present"] is False
    assert row["length"] is None
    assert fetch(client, "congress_gov").status_code == 404


def test_values_survive_a_backend_restart(client):
    put_key(client)
    store: ResearchKeyStore = client.app.state.research_keys
    reopened = ResearchKeyStore(store.path)
    assert reopened.get("congress_gov") == SECRET


# -- validation ---------------------------------------------------------------


@pytest.mark.parametrize("bad", ["Congress", "x-api", "9lives", "a b", "_x"])
def test_name_shape_is_enforced(client, bad):
    resp = client.put(f"/research-keys/{bad}",
                      json={"value": "v-123456"})
    assert resp.status_code == 422, bad


def test_value_whitespace_is_rejected_not_repaired(client):
    """Stripping would MUTATE a secret; we refuse and say why instead."""
    resp = client.put("/research-keys/padded",
                      json={"value": " key-with-space "})
    assert resp.status_code == 422
    assert "whitespace" in resp.json()["detail"]
    resp = client.put("/research-keys/multiline",
                      json={"value": "line1\nline2"})
    assert resp.status_code == 422


# -- the security spine -------------------------------------------------------


def test_backends_own_secrets_are_unreachable(client):
    """The vault reads exactly one file; .env is a different file. Asking
    for the backend's own credentials is a 404, not a disclosure."""
    store: ResearchKeyStore = client.app.state.research_keys
    assert store.path.name == "research-keys.env"
    for name in ("lambda_api_key", "manifold_api_token", "s3_secret_key"):
        assert fetch(client, name).status_code == 404


def test_audit_trail_exists_and_never_carries_the_value(client):
    put_key(client)
    fetch(client, "congress_gov", purpose="tally pipeline auth")
    client.delete("/research-keys/congress_gov")
    rows = client.app.state.orchestrator.db.list_audit(limit=50)
    actions = [r["action"] for r in rows]
    for expected in ("research_key_set", "research_key_read",
                     "research_key_deleted"):
        assert expected in actions, actions
    read_row = next(r for r in rows if r["action"] == "research_key_read")
    assert "tally pipeline auth" in read_row["detail"]
    joined = " ".join(f"{r['action']} {r['detail']}" for r in rows)
    assert SECRET not in joined


def test_agent_audit_route_scrubs_research_key_values(client):
    """Belt-and-suspenders for version-drifted bridges: even a bridge that
    audits set_research_key args verbatim cannot put the value into the
    never-pruned log."""
    resp = client.post("/audit/agent", json={
        "tool": "set_research_key",
        "args": {"name": "x_api", "value": "topsecret999"},
        "note": "", "result": "ok",
    })
    assert resp.status_code == 201
    rows = client.app.state.orchestrator.db.list_audit(limit=5)
    dumped = " ".join(r["detail"] for r in rows if r["action"] == "set_research_key")
    assert "topsecret999" not in dumped
    assert "<redacted, 12 chars>" in dumped


def test_agent_audit_route_scrubs_only_research_key_tools(client):
    """Exact-match scrubbing only: a benign 'value' arg on some other tool
    stays verbatim, because rewriting honest history is worse."""
    client.post("/audit/agent", json={
        "tool": "update_agent_context",
        "args": {"value": "not-a-secret-note"},
        "note": "", "result": "ok",
    })
    rows = client.app.state.orchestrator.db.list_audit(limit=5)
    dumped = " ".join(r["detail"] for r in rows
                      if r["action"] == "update_agent_context")
    assert "not-a-secret-note" in dumped


# -- roles --------------------------------------------------------------------


def test_viewer_may_list_but_never_touch(tmp_path, mock_client, mock_storage,
                                         mock_sidecar):
    app = create_app(
        make_settings(tmp_path, api_token="owner-tok-research"),
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        env_path=tmp_path / ".env",
        research_keys_path=tmp_path / "research-keys.env",
    )
    with TestClient(app, headers={
            "Authorization": "Bearer owner-tok-research"}) as owner:
        minted = owner.post("/principals",
                            json={"name": "watcher", "role": "viewer"})
        assert minted.status_code == 201, minted.text
        put_key(owner)
        viewer = TestClient(app)
        viewer.headers.update(
            {"Authorization": f"Bearer {minted.json()['token']}"})
        assert viewer.get("/research-keys").status_code == 200
        assert viewer.put("/research-keys/other",
                          json={"value": "v-123456"}).status_code == 403
        assert viewer.post("/research-keys/congress_gov/value",
                           json={"purpose": "peek"}).status_code == 403
        assert viewer.delete("/research-keys/congress_gov").status_code == 403
