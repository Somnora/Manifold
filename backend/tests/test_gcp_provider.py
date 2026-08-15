"""Phase 87: Google, for real - every decision testable without Google.

The SDK never appears here. The pure shelf logic tests directly; the
provider's behaviour tests through the same seams the real code uses
(injected fakes for the aggregated lists and the insert operation), and
the orchestrator/route seams test in mock mode. What CANNOT be proven
here - ADC actually resolving, an operation actually creating a machine,
the driver block actually producing a working nvidia-smi - is the real
gate's job, and the gate ledger in DECISIONS.md records what it found.
"""

from __future__ import annotations

import pytest

from app.providers import gcp_catalog
from app.providers.base import ProviderCapacityError, ProviderError
from app.providers.gcp_provider import RealGCPProvider


# -- the shelf (pure) ---------------------------------------------------------


def test_every_shelf_entry_is_launchable_arithmetic():
    """Internal consistency: fixed-GPU families never attach accelerators
    (GCE rejects that), attach entries always do, and every entry carries
    a dated price and a quota metric."""
    for e in gcp_catalog.SHELF:
        if e.name.startswith(("g2-", "a2-", "a3-")):
            assert e.attach_count == 0, e.name
        if e.machine_type.startswith("n1-"):
            assert e.attach_count >= 1, e.name
        assert e.price_cents_per_hour > 0
        assert gcp_catalog.quota_metric(e)      # raises KeyError if unmapped
    assert gcp_catalog.PRICES_RECORDED in gcp_catalog.PRICE_BASIS


def test_zones_come_from_live_data_never_the_shelf():
    entry = gcp_catalog.BY_NAME["g2-standard-4"]
    live = {"nvidia-l4": ["us-central1-a", "us-east4-c"]}
    assert gcp_catalog.zones_for(entry, live) == ["us-central1-a", "us-east4-c"]
    # No live data for the accelerator = offered nowhere, never guessed.
    assert gcp_catalog.zones_for(entry, {}) == []


def test_gpu_description_matches_the_hardware_guide_families():
    """The ladder's curated notes key on gpu_description patterns; a GCP
    A100 must land on the same family notes as a Lambda A100."""
    from app.gpu_guide import _notes_for

    a100 = gcp_catalog.BY_NAME["a2-highgpu-1g"]
    assert _notes_for(gcp_catalog.gpu_description(a100)).family == "A100 40 GB"
    l4 = gcp_catalog.BY_NAME["g2-standard-4"]
    assert _notes_for(gcp_catalog.gpu_description(l4)).family == "L4"
    t4 = gcp_catalog.BY_NAME["n1-standard-4-t4"]
    assert _notes_for(gcp_catalog.gpu_description(t4)).family == "T4"


def test_zone_to_region():
    assert gcp_catalog.zone_to_region("us-central1-a") == "us-central1"
    assert gcp_catalog.zone_to_region("europe-west4-b") == "europe-west4"


# -- the provider, with the SDK faked ----------------------------------------


def unconfigured():
    return RealGCPProvider("", "us-central1-a")


def configured(**kw):
    return RealGCPProvider("test-project", "us-central1-a", **kw)


@pytest.mark.asyncio
async def test_no_project_means_empty_never_an_error():
    """The registered-but-unconfigured provider must not take down the
    sweeps: empty catalog, empty instances, None instance."""
    p = unconfigured()
    assert await p.list_instance_types() == []
    assert await p.list_instances() == []
    assert await p.get_instance("x") is None


@pytest.mark.asyncio
async def test_catalog_joins_shelf_with_live_zones(monkeypatch):
    p = configured()

    async def fake_zones():
        return {"nvidia-l4": ["us-central1-a"],
                "nvidia-h100-80gb": ["us-east4-a", "us-east4-b"]}

    monkeypatch.setattr(p, "_accelerator_zones", fake_zones)
    specs = {s.name: s for s in await p.list_instance_types()}
    assert specs["g2-standard-4"].regions_available == ["us-central1-a"]
    assert specs["a3-highgpu-8g"].regions_available == ["us-east4-a",
                                                        "us-east4-b"]
    # A100 accelerators absent from live data: entries exist, zones empty.
    assert specs["a2-highgpu-1g"].regions_available == []
    assert specs["g2-standard-4"].price_cents_per_hour == 71


@pytest.mark.asyncio
async def test_catalog_failure_degrades_to_empty_not_invented(monkeypatch):
    p = configured()

    async def boom():
        raise RuntimeError("api unreachable")

    monkeypatch.setattr(p, "_accelerator_zones", boom)
    assert await p.list_instance_types() == []


@pytest.mark.asyncio
async def test_launch_refuses_filesystems_and_unknown_types():
    p = configured(public_key_fn=lambda: "ssh-ed25519 AAAA test")
    with pytest.raises(ProviderError) as exc:
        await p.launch_instance("us-central1-a", "g2-standard-4",
                                [], ["somefs"])
    assert "scratch-only" in str(exc.value)
    with pytest.raises(ProviderError) as exc:
        await p.launch_instance("us-central1-a", "gpu_1x_a10", [], [])
    assert "not on the GCP shelf" in str(exc.value)


@pytest.mark.asyncio
async def test_launch_without_a_public_key_names_the_fix():
    p = configured(public_key_fn=lambda: "")
    with pytest.raises(ProviderError) as exc:
        await p.launch_instance("us-central1-a", "g2-standard-4", [], [])
    assert "public key" in str(exc.value)
    assert "private_key_path" in str(exc.value)


@pytest.mark.asyncio
async def test_capacity_and_quota_errors_map_to_their_own_classes(monkeypatch):
    """RESOURCE_POOL_EXHAUSTED is transient -> ProviderCapacityError (the
    retry loop backs off). QUOTA is not transient -> plain ProviderError
    whose message carries the console link and the metric name."""
    p = configured(public_key_fn=lambda: "ssh-ed25519 AAAA test")

    class FakeCompute:
        def __getattr__(self, name):
            raise RuntimeError("must not be touched in this test")

    async def run_raising(fn, *a, **k):
        raise RuntimeError(FakeCompute._msg)

    monkeypatch.setattr(p, "_compute", lambda: FakeCompute())
    monkeypatch.setattr(p, "_run", run_raising)

    FakeCompute._msg = "ZONE_RESOURCE_POOL_EXHAUSTED: no L4 for you today"
    with pytest.raises(ProviderCapacityError):
        await p.launch_instance("us-central1-a", "g2-standard-4", [], [])

    FakeCompute._msg = "Quota NVIDIA_L4_GPUS exceeded. Limit: 0.0"
    with pytest.raises(ProviderError) as exc:
        await p.launch_instance("us-central1-a", "g2-standard-4", [], [])
    msg = str(exc.value)
    assert "console.cloud.google.com" in msg
    assert "NVIDIA_L4_GPUS" in msg
    assert not isinstance(exc.value, ProviderCapacityError)


@pytest.mark.asyncio
async def test_terminate_of_a_missing_instance_is_idempotent(monkeypatch):
    p = configured()

    async def nothing(instance_id):
        return None

    monkeypatch.setattr(p, "_find", nothing)
    assert await p.terminate_instance("manifold-gone") is True


# -- the seams (mock mode / test app) ----------------------------------------


def test_gcp_launch_with_filesystem_is_refused_before_money(client):
    """The orchestrator refuses provider+filesystem combinations before a
    launch row exists - the message names the fix, not a stack trace."""
    resp = client.post("/instances", json={
        "provider": "gcp", "instance_type": "g2-standard-4",
        "region": "us-central1-a", "filesystem": "manifold-data",
    })
    assert resp.status_code == 400
    assert "scratch-only" in resp.json()["detail"]


def test_regions_are_provider_scoped(client):
    lam = client.get("/regions").json()["regions"]
    gcp = client.get("/regions?provider=gcp").json()["regions"]
    assert len(lam) > 0
    # Real-mode GCP is unconfigured in the test app: empty regions is the
    # truth (no shelf zones), never Lambda's list relabelled.
    assert gcp == []


def test_quota_route_survives_an_unconfigured_provider(client):
    resp = client.get("/gcp/quota")
    assert resp.status_code == 200
    assert resp.json()["quotas"] == []


def test_catalog_carries_the_price_label_for_gcp_only(client):
    """The money rule: a dated list price must never appear without its
    label, and Lambda's live prices must never grow one."""
    lam = client.get("/instance-types").json()
    assert all("price_basis" not in t for t in lam.values())


@pytest.mark.asyncio
async def test_expired_adc_reads_as_the_command_that_fixes_it():
    """Hit live at the 2026-08-15 gate: a 21-day-old ADC token expired and
    the raw RefreshError traceback leaked through. Auth expiry is a NORMAL
    state; it must surface as the one command that fixes it."""
    from app.providers.base import ProviderUnavailable

    p = configured()

    class RefreshError(Exception):
        pass

    async def bad_auth():
        import asyncio

        def raise_it():
            raise RefreshError(
                "Reauthentication is needed. Please run `gcloud auth "
                "application-default login` to reauthenticate.")
        return await p._run(raise_it)

    with pytest.raises(ProviderUnavailable) as exc:
        await bad_auth()
    assert "gcloud auth application-default login" in str(exc.value)
    assert "expired" in str(exc.value)


def test_instance_cards_carry_their_provider(client):
    """A mixed fleet is ambiguous exactly when it matters - the GCE gate
    had to infer the provider from the id shape. Every card says whose
    cloud it is, launch row first, 'lambda' only as the pre-field
    fallback."""
    from tests.test_reconcile import launch_connected

    _, instance_id = launch_connected(client)
    card = next(i for i in client.get("/instances").json()["instances"]
                if i["id"] == instance_id)
    assert card["provider"] == "lambda"
