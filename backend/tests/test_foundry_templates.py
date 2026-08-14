"""Phase 83: the Foundry templates.

Three bundled recipes make "train your own model" literally true:
lerobot-act (a robot policy from random weights on your own episodes),
smolvla-finetune (language-conditioned, public base, no token), and
nanogpt-pretrain (a GPT from zero that samples its own output into the
job log). No new backend surface - the whole phase is recipes riding
machinery that already exists, so what these tests pin is the recipes:
they load through the mount jail, their rendered docker commands stay
golden, and they dispatch + chain like any other job.
"""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import IdleSettings, TaskSettings
from app.dispatcher import coerce_parameters, render_docker_command
from app.main import create_app
from app.templates import load_templates
from tests.conftest import make_settings, mock_connect_fn

BUNDLED = Path(__file__).resolve().parents[2] / "templates"


@pytest.fixture(scope="module")
def foundry():
    templates, errors = load_templates(BUNDLED)
    assert not errors, f"bundled templates failed to load: {errors}"
    return templates


FOUNDRY_NAMES = ("lerobot-act", "smolvla-finetune", "nanogpt-pretrain")


def test_foundry_templates_load_through_the_jail(foundry):
    assert set(FOUNDRY_NAMES) <= set(foundry)
    for name in FOUNDRY_NAMES:
        t = foundry[name]
        # Training jobs publish nothing and listen on nothing.
        assert t.ports == []
        assert t.network == ""
        # Every mount is inside the jail (the loader enforces this - the
        # assertion documents it where the Foundry reader will look).
        for v in t.volumes:
            assert v.host.startswith(("{persistent}", "/workspace/ephemeral"))


def test_dataset_templates_require_only_the_dataset(foundry):
    for name in ("lerobot-act", "smolvla-finetune"):
        t = foundry[name]
        required = [p.name for p in t.parameters if p.required]
        assert required == ["dataset"], name
    # nanogpt is fully defaulted: the cheap path needs zero decisions.
    assert [p.name for p in foundry["nanogpt-pretrain"].parameters
            if p.required] == []


def test_lerobot_act_render_is_golden(foundry):
    t = foundry["lerobot-act"]
    params = coerce_parameters(t, {"dataset": "pusht", "steps": 2000,
                                   "save_freq": 1000})
    cmd = render_docker_command(t, params, filesystem="manifold-data",
                                task_id="t1")
    # --user 0:0: the LeRobot image drops to uid 1001, which cannot write
    # the NFS bind mounts (checkpoints died at the real gate). The knob
    # restores docker's root default; only "root" is accepted at load.
    assert cmd.startswith(
        "docker run --rm --name manifold-task-t1 --gpus all --user 0:0")
    # The mounts, exactly: datasets ro, outputs rw, shared HF cache.
    assert "-v /lambda/nfs/manifold-data/datasets:/data/datasets:ro" in cmd
    assert "-v /lambda/nfs/manifold-data/outputs:/data/output" in cmd
    assert ("-v /lambda/nfs/manifold-data/cache/huggingface:"
            "/root/.cache/huggingface" in cmd)
    # The training invocation, exactly: from-scratch ACT, this dataset,
    # wandb off (an unattended job must never wait on a login prompt).
    # lerobot-train entry point and NO --policy.device: both verified
    # against the live image at the 2026-08-14 real gate (the module path
    # python -m lerobot.scripts.train no longer exists, and device is
    # auto-selected - the flag would error as unknown).
    assert "lerobot-train --policy.type=act" in cmd
    assert "--dataset.repo_id=pusht --dataset.root=/data/datasets/pusht" in cmd
    assert "--steps=2000 --batch_size=8 --save_freq=1000" in cmd
    assert "--output_dir=/data/output/act-run" in cmd
    # Never push to the Hub from an unattended job (current LeRobot
    # DEFAULTS to pushing; validation demands repo_id for it - both
    # learned at the real gate). Same philosophy as wandb off.
    assert "--policy.push_to_hub=false" in cmd
    assert "--policy.repo_id=local/act-run" in cmd
    assert "--wandb.enable=false" in cmd
    assert "huggingface/lerobot-gpu:latest" in cmd


def test_smolvla_render_pulls_the_public_base(foundry):
    t = foundry["smolvla-finetune"]
    params = coerce_parameters(t, {"dataset": "desk-clear"})
    cmd = render_docker_command(t, params, filesystem="manifold-data",
                                task_id="t2")
    # Fine-tune FROM the public base - policy.path, not policy.type.
    assert "--policy.path=lerobot/smolvla_base" in cmd
    assert "--dataset.repo_id=desk-clear" in cmd
    assert "--steps=20000 --batch_size=4" in cmd
    assert "-e HF_HOME=/root/.cache/huggingface" in cmd


def test_nanogpt_render_trains_and_then_speaks(foundry):
    t = foundry["nanogpt-pretrain"]
    params = coerce_parameters(t, {"max_iters": 200})
    cmd = render_docker_command(t, params, filesystem="manifold-data",
                                task_id="t3")
    assert "git clone --depth 1 https://github.com/karpathy/nanoGPT" in cmd
    assert "--max_iters=200" in cmd
    assert "--compile=False" in cmd
    # The last act samples from the model just trained: generated text
    # lands in the job log as the proof of life.
    assert "python sample.py --out_dir=/data/output/nanogpt-run" in cmd
    # Pinned base image: the one Foundry template with no drift warning.
    assert "pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime" in cmd
    assert t.warnings == []


def test_lerobot_images_carry_the_drift_warning(foundry):
    for name in ("lerobot-act", "smolvla-finetune"):
        assert any("floating" in w for w in foundry[name].warnings), name


# -- dispatch: a Foundry job is just a job ------------------------------------


@pytest.fixture
def fast_app(tmp_path, mock_client, mock_storage, mock_sidecar):
    settings = make_settings(
        tmp_path,
        tasks=TaskSettings(poll_seconds=0.02),
        idle=IdleSettings(timeout_seconds=30.0, poll_seconds=0.5),
    )
    return create_app(
        settings,
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
    )


def wait_until(predicate, timeout=8.0, interval=0.02, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}")


def test_training_pipeline_dispatches_and_chains(fast_app):
    """gpu-smoke -> lerobot-act via depends_on: the data-prep-then-train
    pipeline shape from the walkthrough, proven over HTTP against a mock
    instance - held while the parent runs, rendered correctly, ordered."""
    with TestClient(fast_app) as client:
        client.post("/instances", json={
            "instance_type": "gpu_1x_a10", "region": "us-east-1",
            "filesystem": "manifold-data",
        })
        prep = client.post("/tasks", json={
            "template": "gpu-smoke", "parameters": {},
        }).json()["task"]["id"]
        train = client.post("/tasks", json={
            "template": "lerobot-act",
            "parameters": {"dataset": "pusht", "steps": 2000},
            "depends_on": [prep],
        }).json()["task"]["id"]

        done = wait_until(
            lambda: (t := client.get(f"/tasks/{train}").json())["status"]
            == "succeeded" and t,
            message="training job completion",
        )
        parent = client.get(f"/tasks/{prep}").json()
        assert done["started_at"] >= parent["finished_at"]

        lines = " ".join(
            l["line"] for l in
            client.get(f"/tasks/{train}/logs").json()["lines"])
        assert "--policy.type=act" in lines
        assert "-v /lambda/nfs/manifold-data/datasets:/data/datasets:ro" in lines
        assert done["output_paths"], "training outputs should be declared"
