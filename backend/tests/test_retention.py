"""Phase 98: telemetry retention - the table with a hot reader stops
growing forever.

telemetry_samples gains a row per connected instance every 30s and, since
Phase 96, is read on every /instances poll (latest_telemetry). Nothing
ever pruned it. The sweep drops samples older than telemetry.retain_days
- 30 by default, deliberately equal to max_lifetime_max_seconds, so no
live launch can outlive its own telemetry (idle-spend accounting reads
samples across a launch's whole window).

audit_log is NEVER pruned. It is the forensic record: reconstructing one
night of terminations depended on rows nobody knew they would need.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tests.test_idle_matrix import Harness


def sample_at(db, instance_id, days_ago):
    at = (datetime.now(timezone.utc)
          - timedelta(days=days_ago)).isoformat(timespec="seconds")
    db.record_telemetry_sample(instance_id, gpu_name="A100",
                               vram_used_mib=1, vram_total_mib=2,
                               util_pct=0, at=at)


def test_prune_drops_old_and_keeps_new(db):
    sample_at(db, "i-old", 45)
    sample_at(db, "i-new", 1)
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=30)).isoformat(timespec="seconds")
    assert db.prune_telemetry(cutoff) == 1
    assert db.latest_telemetry(["i-new"]).get("i-new") is not None
    assert db.latest_telemetry(["i-old"]) == {}


def test_the_knob_is_loader_readable(tmp_path):
    """Born readable - the busy_util_pct lesson, applied at birth."""
    from app.config import load_settings
    (tmp_path / "config.yaml").write_text("telemetry:\n  retain_days: 7\n")
    settings = load_settings(config_path=tmp_path / "config.yaml",
                             env_path=tmp_path / ".env")
    assert settings.telemetry.retain_days == 7.0


async def test_the_sweep_prunes_hourly_and_audits_once(tmp_path, db):
    harness = Harness(tmp_path, db)
    sample_at(db, "i-ancient", 45)
    sample_at(db, "i-ancient", 44)
    await harness.dispatcher._sample_telemetry_once()
    rows = [r for r in db.list_audit(limit=10)
            if r["action"] == "telemetry_pruned"]
    assert len(rows) == 1
    assert "2 sample(s)" in rows[0]["detail"]
    # A second pass inside the hour does not prune (or audit) again.
    sample_at(db, "i-ancient2", 45)
    await harness.dispatcher._sample_telemetry_once()
    rows = [r for r in db.list_audit(limit=10)
            if r["action"] == "telemetry_pruned"]
    assert len(rows) == 1


async def test_zero_disables_retention(tmp_path, db):
    from dataclasses import replace
    harness = Harness(tmp_path, db)
    harness.dispatcher.settings = replace(
        harness.dispatcher.settings,
        telemetry=replace(harness.dispatcher.settings.telemetry,
                          retain_days=0))
    sample_at(db, "i-forever", 400)
    await harness.dispatcher._sample_telemetry_once()
    assert db.latest_telemetry(["i-forever"]).get("i-forever") is not None


def test_audit_log_has_no_prune_path():
    """Pinned by absence: no db method deletes from audit_log. If someone
    adds one, they argue with this test and the night that needed the
    rows."""
    import inspect

    from app import db as dbmod
    src = inspect.getsource(dbmod)
    assert "DELETE FROM audit_log" not in src
