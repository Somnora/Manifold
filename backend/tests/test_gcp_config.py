"""Phase 105: the GCP config form works for the auth path the docs teach.

Found live on 2026-08-18, the first time a real project was configured:
GCPConfigRequest required `credentials_file` with min_length=8, but the
PRIMARY documented path is ADC (`gcloud auth application-default login`)
with no file at all - so the Settings form 422'd on exactly the setup it
exists for, and an empty GOOGLE_APPLICATION_CREDENTIALS, had it saved,
would have overridden working ADC with a broken pointer.
"""


def read_env(client) -> str:
    import re
    # The env file the route writes is the one create_app was handed.
    for route in client.app.routes:
        pass
    return None


def env_path_of(client):
    from pathlib import Path
    # conftest passes env_path=... only in some fixtures; the settings
    # status route publishes where secrets go.
    status = client.get("/settings/status").json()
    return Path(status["env_path"])


def test_adc_only_config_saves_project_and_nothing_else(client):
    resp = client.post("/settings/gcp-config",
                       json={"project_id": "somnora-dev-01"})
    assert resp.status_code == 200, resp.text
    text = env_path_of(client).read_text()
    assert "GCP_PROJECT_ID=somnora-dev-01" in text
    # The dangerous half of the old behavior: an empty credentials pointer
    # must never be written.
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in text
    assert "GCP_DEFAULT_ZONE" not in text


def test_empty_strings_mean_omitted(client):
    """The dashboard sends trimmed strings; "" must behave as absent."""
    resp = client.post("/settings/gcp-config",
                       json={"project_id": "somnora-dev-01",
                             "default_zone": "", "credentials_file": ""})
    assert resp.status_code == 200, resp.text
    text = env_path_of(client).read_text()
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in text


def test_a_named_credentials_file_must_exist(client, tmp_path):
    resp = client.post("/settings/gcp-config",
                       json={"project_id": "p-1234",
                             "credentials_file": "/nope/creds.json"})
    assert resp.status_code == 422
    assert "/nope/creds.json" in resp.json()["detail"]
    assert "ADC" in resp.json()["detail"]   # the error teaches the fix

    real = tmp_path / "sa.json"
    real.write_text("{}")
    resp = client.post("/settings/gcp-config",
                       json={"project_id": "p-1234",
                             "credentials_file": str(real)})
    assert resp.status_code == 200, resp.text
    assert f"GOOGLE_APPLICATION_CREDENTIALS={real}" in env_path_of(client).read_text()


def test_zone_is_written_only_when_given(client):
    resp = client.post("/settings/gcp-config",
                       json={"project_id": "p-1234",
                             "default_zone": "us-central1-a"})
    assert resp.status_code == 200
    assert "GCP_DEFAULT_ZONE=us-central1-a" in env_path_of(client).read_text()
