"""Phase 99: hand-started model servers become first-class citizens.

THE GAP, from the heaviest user's review (#2, their last first-tier item):
a server the templates could not express got started by hand over SSH,
and that single fallback cost proxy routing (no_model_served), activity
visibility (the 07:42 reap: terminated at 100% util because nothing
Manifold-visible was using it), log streaming, and restart supervision.
extra_args closed the "could not express" half; register_endpoint closes
the rest: with both in place, "invisible busy server" stops being a
construct that can exist.

The port is a LOOPBACK port on the instance, reached only over the
managed SSH connection. Nothing new listens anywhere; the
nothing-on-the-network rule is untouched.
"""

import pytest

from tests.test_terminal import launch_connected

AUTH = {}  # the client fixture runs an open backend


def register(client, instance_id, port=8801, model_id="qwen-live",
             note="hand-started for the extraction run"):
    resp = client.post(f"/instances/{instance_id}/endpoints",
                       json={"port": port, "model_id": model_id,
                             "note": note})
    assert resp.status_code == 201, resp.text
    return resp.json()


# -- the proxy adopts it ------------------------------------------------------


def test_a_registered_server_appears_in_v1_models(client):
    instance_id = launch_connected(client)
    register(client, instance_id)
    models = client.get("/v1/models").json()["data"]
    assert any(m["id"] == "qwen-live" for m in models), (
        "the registered endpoint did not reach the proxy's model list")


def test_completions_route_to_it_and_count_as_activity(client):
    instance_id = launch_connected(client)
    register(client, instance_id)
    dispatcher = client.app.state.dispatcher
    dispatcher.last_activity.pop(instance_id, None)

    resp = client.post("/v1/chat/completions", json={
        "model": "qwen-live",
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert resp.status_code == 200, resp.text

    assert instance_id in dispatcher.last_activity, (
        "proxy traffic through a registered endpoint must reset the idle "
        "clock - invisible-busy-server is the bug this feature removes")


def test_instance_id_pinning_still_works(client):
    instance_id = launch_connected(client)
    register(client, instance_id)
    resp = client.post("/v1/chat/completions", json={
        "model": instance_id,
        "messages": [{"role": "user", "content": "pin by box"}],
    })
    assert resp.status_code == 200, resp.text


# -- lifecycle ----------------------------------------------------------------


def test_reregistering_a_port_updates_the_model(client):
    """Restarting a server on the same port is the normal case, not a
    conflict."""
    instance_id = launch_connected(client)
    register(client, instance_id, model_id="v1-model")
    register(client, instance_id, model_id="v2-model")
    rows = client.get(f"/instances/{instance_id}/endpoints").json()["endpoints"]
    assert len(rows) == 1
    assert rows[0]["model_id"] == "v2-model"


def test_deregister_removes_it_from_the_proxy(client):
    instance_id = launch_connected(client)
    register(client, instance_id)
    assert client.delete(
        f"/instances/{instance_id}/endpoints/8801").status_code == 200
    models = client.get("/v1/models").json()["data"]
    assert not any(m["id"] == "qwen-live" for m in models)
    assert client.delete(
        f"/instances/{instance_id}/endpoints/8801").status_code == 404


def test_the_server_itself_is_not_touched_by_deregister(client):
    """Deregistering stops ROUTING; it must not run anything on the box."""
    instance_id = launch_connected(client)
    register(client, instance_id)
    conn = client.app.state.orchestrator.connections[instance_id]._conn
    before = list(conn.commands)
    client.delete(f"/instances/{instance_id}/endpoints/8801")
    assert conn.commands == before, "deregister reached into the instance"


def test_termination_cleans_up_the_registration(client):
    """A route to a gone instance that still looks like a served model is
    exactly the stale-fact class this codebase exists to not have."""
    instance_id = launch_connected(client)
    register(client, instance_id)
    client.app.state.orchestrator.connections.pop(instance_id, None)
    assert client.delete(f"/instances/{instance_id}").status_code == 200
    db = client.app.state.orchestrator.db
    assert db.list_registered_endpoints(instance_id) == []
    models = client.get("/v1/models").json()["data"]
    assert not any(m["id"] == "qwen-live" for m in models)


def test_a_disconnected_instance_is_not_served(client):
    """The row may exist, but a box we cannot reach is not offered as a
    model: offering it would hand clients a route to a timeout."""
    instance_id = launch_connected(client)
    register(client, instance_id)
    client.app.state.orchestrator.connections.pop(instance_id, None)
    models = client.get("/v1/models").json()["data"]
    assert not any(m["id"] == "qwen-live" for m in models)


# -- validation ---------------------------------------------------------------


def test_registering_needs_a_connected_instance(client):
    resp = client.post("/instances/nonexistent/endpoints",
                       json={"port": 8801, "model_id": "m"})
    assert resp.status_code == 409


def test_port_bounds_are_enforced(client):
    instance_id = launch_connected(client)
    for bad in (0, 65536, -1):
        resp = client.post(f"/instances/{instance_id}/endpoints",
                           json={"port": bad, "model_id": "m"})
        assert resp.status_code == 422, f"port {bad} was accepted"


def test_registration_is_audited_with_the_note(client):
    instance_id = launch_connected(client)
    register(client, instance_id, note="Tally extraction server")
    rows = client.app.state.orchestrator.db.list_audit(limit=10)
    hits = [r for r in rows if r["action"] == "endpoint_registered"]
    assert hits and "Tally extraction server" in hits[0]["detail"]
