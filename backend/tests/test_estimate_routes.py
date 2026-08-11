"""Estimate + utilization endpoints against the mock app, with seeded
history and telemetry — the shape Gate C demonstrates."""

from datetime import datetime, timedelta, timezone


def _seed_launch(db, instance_id, gpu_type="gpu_1x_a10", rate_cents=129):
    lid = db.create_launch(
        requested_type=gpu_type, region="us-east-1",
        filesystem="manifold-data", connection_mode="direct-ssh",
        hourly_rate_cents=rate_cents,
    )
    started = datetime(2026, 7, 11, 20, 0, tzinfo=timezone.utc)
    db.update_launch(
        lid, status="terminated", lambda_instance_id=instance_id,
        launched_type=gpu_type,
        launched_at=started.isoformat(timespec="seconds"),
        terminated_at=(started + timedelta(minutes=45)).isoformat(
            timespec="seconds"),
    )
    return lid


def _seed_runs(db, instance_id, template, minutes_each, count):
    base = datetime(2026, 7, 11, 20, 0, tzinfo=timezone.utc)
    for i in range(count):
        tid = db.create_task(template=template, parameters={})
        s = base + timedelta(hours=i)
        db.update_task(
            tid, status="succeeded", instance_id=instance_id,
            started_at=s.isoformat(timespec="seconds"),
            finished_at=(s + timedelta(minutes=minutes_each)).isoformat(
                timespec="seconds"),
            exit_code=0,
        )


def test_estimate_with_history_is_measured(client):
    db = client.app.state.orchestrator.db
    _seed_launch(db, "i-hist")
    _seed_runs(db, "i-hist", "whisper-batch", minutes_each=40, count=5)

    r = client.get("/estimate",
                   params={"template": "whisper-batch",
                           "instance_type": "gpu_1x_a10"})
    assert r.status_code == 200
    e = r.json()
    assert e["confidence"] == "measured"
    assert e["minutes"] == 40.0
    assert e["cost_usd"] == 0.86        # 40 min * $1.29/hr
    assert e["sample_size"] == 5


def test_estimate_without_history_is_rough(client):
    # No runs recorded for this GPU type -> coarse default, marked rough.
    r = client.get("/estimate",
                   params={"template": "whisper-batch",
                           "instance_type": "gpu_1x_a100_sxm4"})
    assert r.status_code == 200
    e = r.json()
    assert e["confidence"] == "rough"
    assert e["sample_size"] == 0
    assert "no history" in e["basis"]


def test_estimate_unknown_template_404(client):
    r = client.get("/estimate",
                   params={"template": "nope", "instance_type": "gpu_1x_a10"})
    assert r.status_code == 404


def test_utilization_fires_right_size_hint(client):
    db = client.app.state.orchestrator.db
    lid = _seed_launch(db, "i-util")
    # 30 samples peaking at 9 GB on a 24 GB card -> clearly underused.
    for i in range(30):
        db.record_telemetry_sample(
            "i-util", gpu_name="A10",
            vram_used_mib=6000 + (i % 5) * 700,   # peaks ~8800 MiB (~8.6 GB)
            vram_total_mib=24564, util_pct=12 + (i % 3) * 4)
    r = client.get(f"/launches/{lid}/utilization")
    assert r.status_code == 200
    u = r.json()
    assert u["available"] is True
    assert "peak VRAM" in u["verdict"]
    assert "45 min" in u["verdict"]
    assert u["right_size_hint"] is True
    assert "smaller" in u["hint"].lower()


def test_utilization_no_hint_when_card_well_used(client):
    db = client.app.state.orchestrator.db
    lid = _seed_launch(db, "i-busy")
    for i in range(30):
        db.record_telemetry_sample(
            "i-busy", gpu_name="A10",
            vram_used_mib=21000, vram_total_mib=24564, util_pct=88)
    u = client.get(f"/launches/{lid}/utilization").json()
    assert u["right_size_hint"] is False
    assert "smaller" not in u["hint"].lower()


def test_utilization_reports_idle_spend_beside_the_hint(client):
    """Both numbers on one response, reading DIFFERENT columns on purpose:
    the hint keys on the per-sample max, idle spend on the per-sample mean.
    The 45-minute seeded launch was idle for 30 of them."""
    db = client.app.state.orchestrator.db
    lid = _seed_launch(db, "i-idle")
    started = datetime(2026, 7, 11, 20, 0, tzinfo=timezone.utc)
    for i in range(60):                       # 30 min of samples, 30s apart
        db.record_telemetry_sample(
            "i-idle", gpu_name="A10", vram_used_mib=6000,
            vram_total_mib=24564, util_pct=99, util_pct_mean=1.0, gpu_count=8,
            at=(started + timedelta(seconds=30 * i)).isoformat(
                timespec="seconds"))

    idle = client.get(f"/launches/{lid}/utilization").json()["idle_spend"]
    assert idle["measured"] is True
    assert idle["idle_seconds"] == 1800
    assert idle["idle_usd"] == 0.65           # 30 min at $1.29/hr
    # The 15 minutes nobody sampled stay unknown, never idle.
    assert idle["unknown_seconds"] == 900
    assert idle["window_end_basis"] == "terminated_at"


def test_utilization_idle_spend_unmeasured_without_telemetry(client):
    """No samples is "not measured", never "$0.00 of idle spend"."""
    db = client.app.state.orchestrator.db
    lid = _seed_launch(db, "i-nosamples")
    idle = client.get(f"/launches/{lid}/utilization").json()["idle_spend"]
    assert idle["measured"] is False
    assert idle["idle_seconds"] is None and idle["idle_usd"] is None
    assert idle["unknown_seconds"] == 2700


def test_utilization_unavailable_without_telemetry(client):
    db = client.app.state.orchestrator.db
    lid = _seed_launch(db, "i-quiet")   # no samples recorded
    u = client.get(f"/launches/{lid}/utilization").json()
    assert u["available"] is False


def test_sampler_records_from_a_connected_instance(client):
    """The telemetry loop reads the sidecar and persists a sample. Drive one
    tick directly against a connected mock instance."""
    import asyncio
    from tests.test_reconcile import launch_connected

    _, instance_id = launch_connected(client)
    dispatcher = client.app.state.dispatcher
    db = client.app.state.orchestrator.db

    before = db.telemetry_summary(instance_id)["sample_count"]
    asyncio.run(dispatcher._sample_telemetry_once())
    after = db.telemetry_summary(instance_id)
    assert after["sample_count"] == before + 1
    assert after["vram_total_mib"] == 24564          # from the mock sidecar
    assert after["peak_vram_used_mib"] > 0
