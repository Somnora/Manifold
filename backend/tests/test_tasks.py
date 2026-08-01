"""Task queue, parameter validation, and docker-command rendering."""

import pytest

from app.dispatcher import (
    ParameterError,
    coerce_parameters,
    output_paths_for,
    render_docker_command,
)
from app.templates import load_templates
from pathlib import Path

REPO_TEMPLATES = Path(__file__).resolve().parent.parent.parent / "templates"
TEMPLATES, _ = load_templates(REPO_TEMPLATES)


def test_coerce_applies_defaults_and_types():
    vllm = TEMPLATES["vllm-serve"]
    params = coerce_parameters(vllm, {"model_id": "meta-llama/Llama-3.1-8B"})
    assert params["model_id"] == "meta-llama/Llama-3.1-8B"
    assert params["max_context"] == 8192
    assert params["port"] == 8080
    assert params["tensor_parallel"] == 1
    assert params["tool_call_parser"] == "hermes"
    assert params["enable_lora"] is False
    assert params["max_loras"] == 4
    # String numbers are coerced to their declared type.
    params = coerce_parameters(
        vllm, {"model_id": "m", "max_context": "4096"}
    )
    assert params["max_context"] == 4096


def test_coerce_reports_all_problems_at_once():
    vllm = TEMPLATES["vllm-serve"]
    with pytest.raises(ParameterError) as exc:
        coerce_parameters(vllm, {"max_context": "lots", "bogus": 1})
    message = str(exc.value)
    assert "missing required parameter 'model_id'" in message
    assert "must be integer" in message
    assert "unknown parameter 'bogus'" in message


def test_render_docker_command_quotes_and_substitutes():
    vllm = TEMPLATES["vllm-serve"]
    params = coerce_parameters(
        vllm, {"model_id": "org/model; rm -rf /", "port": 9000}
    )
    cmd = render_docker_command(
        vllm, params, filesystem="manifold-data", task_id="t1"
    )
    # Injection attempt arrives shell-quoted, inert.
    assert "'org/model; rm -rf /'" in cmd
    # {persistent} resolved to the filesystem mount.
    assert "-v /lambda/nfs/manifold-data/cache/huggingface:/root/.cache/huggingface" in cmd
    # Ports always published on loopback only.
    assert "-p 127.0.0.1:8080:8080" in cmd
    assert "--gpus all" in cmd
    assert "--name manifold-task-t1" in cmd
    assert "hermes" in cmd


def test_render_substitutes_parameters_in_mounts():
    whisper = TEMPLATES["whisper-batch"]
    params = coerce_parameters(whisper, {"input_dir": "interviews/day2"})
    cmd = render_docker_command(
        whisper, params, filesystem="manifold-data", task_id="t2"
    )
    assert "-v /lambda/nfs/manifold-data/interviews/day2:/data/input:ro" in cmd


def test_output_paths_are_writable_persistent_mounts():
    whisper = TEMPLATES["whisper-batch"]
    params = coerce_parameters(whisper, {})
    paths = output_paths_for(whisper, params, "manifold-data")
    # Input is read-only, so outputs are transcripts + HF cache.
    assert "/lambda/nfs/manifold-data/transcripts" in paths
    assert all(not p.endswith("/inbox") for p in paths)


# -- queue endpoints ---------------------------------------------------------------


def test_enqueue_validates_at_the_door(client):
    resp = client.post("/tasks", json={
        "template": "vllm-serve",
        "parameters": {"max_context": "not-a-number"},
    })
    assert resp.status_code == 422
    assert "model_id" in resp.json()["detail"]

    resp = client.post("/tasks", json={"template": "no-such-template"})
    assert resp.status_code == 404


def test_enqueue_and_read_back(client):
    resp = client.post("/tasks", json={
        "template": "whisper-batch",
        "parameters": {"input_dir": "inbox", "model_size": "small"},
    })
    assert resp.status_code == 202
    task = resp.json()["task"]
    assert task["status"] == "queued"
    assert task["parameters"] == {"input_dir": "inbox", "model_size": "small"}

    listed = client.get("/tasks").json()["tasks"]
    assert any(t["id"] == task["id"] for t in listed)

    logs = client.get(f"/tasks/{task['id']}/logs").json()
    assert logs["lines"] == []          # nothing dispatched yet


def test_task_logs_tail(db):
    from app.task_queue import SQLiteTaskQueue
    queue = SQLiteTaskQueue(db)
    task_id = queue.enqueue(template="whisper-batch", parameters={})
    for i in range(10):
        queue.append_log(task_id, f"line {i}")
    tail = queue.get_logs(task_id, tail=3)
    assert [l["line"] for l in tail] == ["line 7", "line 8", "line 9"]
    assert [l["seq"] for l in tail] == [7, 8, 9]


def test_task_logs_tail_over_http(client):
    """The failed-job card fetches GET /tasks/{id}/logs?tail=10 to show WHY a
    job failed inline. The endpoint must return exactly the LAST N lines."""
    db = client.app.state.orchestrator.db
    task_id = db.create_task(template="gpu-smoke", parameters={"note": "x"})
    for i in range(1, 26):
        db.append_task_log(task_id, f"line {i}")
    db.update_task(task_id, status="failed", exit_code=126,
                   error="container exited 126")
    lines = [l["line"] for l in
             client.get(f"/tasks/{task_id}/logs?tail=10").json()["lines"]]
    assert lines == [f"line {i}" for i in range(16, 26)]
