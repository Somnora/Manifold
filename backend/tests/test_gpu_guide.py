"""Phase 86: the hardware ladder.

The rule under test is the same one the spend pages live by: numbers come
from the provider or from labelled arithmetic on provider numbers - never
from this repo's prose. A hardcoded price survived on the cluster screen
for a month; these tests make that structurally impossible for the guide by
asserting every number in the output traces to the catalog fed in.
"""

from __future__ import annotations

import re

from app import gpu_guide


def catalog(**overrides):
    base = {
        "cpu_4x_general": {
            "description": "4x CPU General (16 GiB)",
            "gpu_description": "N/A",
            "price_usd_per_hour": 0.20,
            "specs": {"vcpus": 4, "memory_gib": 16, "storage_gib": 100,
                      "gpus": 0},
            "regions_with_capacity": ["us-east-1"],
        },
        "gpu_1x_a10": {
            "description": "1x A10 (24 GB PCIe)",
            "gpu_description": "A10 (24 GB PCIe)",
            "price_usd_per_hour": 1.29,
            "specs": {"vcpus": 30, "memory_gib": 200, "storage_gib": 1400,
                      "gpus": 1},
            "regions_with_capacity": ["us-east-1"],
        },
        "gpu_8x_b200_sxm6": {
            "description": "8x B200 (180 GB SXM6)",
            "gpu_description": "B200 (180 GB SXM6)",
            "price_usd_per_hour": 53.52,
            "specs": {"vcpus": 224, "memory_gib": 4000,
                      "storage_gib": 28000, "gpus": 8},
            "regions_with_capacity": [],
        },
    }
    base.update(overrides)
    return base


def test_cpu_types_are_not_on_a_gpu_ladder():
    guide = gpu_guide.build_guide(catalog())
    assert all(r["gpu_count"] >= 1 for r in guide["rungs"])
    assert not any("cpu" in r["instance_type"] for r in guide["rungs"])


def test_every_number_traces_to_the_catalog():
    """Prices, VRAM and capacity are passthrough or arithmetic - the guide
    must be unable to disagree with the launch form beside it."""
    guide = gpu_guide.build_guide(catalog())
    a10 = next(r for r in guide["rungs"] if r["instance_type"] == "gpu_1x_a10")
    assert a10["price_usd_per_hour"] == 1.29           # passthrough
    assert a10["vram_per_gpu_gib"] == 24               # parsed from provider
    assert a10["vram_total_gib"] == 24
    assert a10["available_now"] is True
    assert a10["price_per_gib_hour"] == round(1.29 / 24, 4)

    b200 = next(r for r in guide["rungs"]
                if r["instance_type"] == "gpu_8x_b200_sxm6")
    assert b200["vram_total_gib"] == 8 * 180
    assert b200["available_now"] is False              # empty regions
    assert b200["price_per_gib_hour"] == round(53.52 / 1440, 4)


def test_no_price_literal_hides_in_the_module():
    """The $24.72 rule, enforced at the source level: gpu_guide.py may not
    contain a dollar amount. If a price appears in the guide's output it
    came from the catalog, because there is nowhere else it could come
    from."""
    import inspect

    source = inspect.getsource(gpu_guide)
    assert not re.search(r"\$\s*\d+[\d.,]*", source), (
        "gpu_guide.py contains what looks like a hardcoded price")


def test_the_ladder_climbs_by_total_memory():
    guide = gpu_guide.build_guide(catalog())
    totals = [r["vram_total_gib"] for r in guide["rungs"]]
    assert totals == sorted(totals)
    assert totals[-1] == 1440                          # 8x B200 on top


def test_fits_arithmetic_matches_its_stated_basis():
    """The numbers must be exactly what FITS_BASIS says they are, so the
    UI's 'show its work' line is never a lie."""
    guide = gpu_guide.build_guide(catalog())
    a10 = next(r for r in guide["rungs"] if r["instance_type"] == "gpu_1x_a10")
    usable = 24 * 0.8
    assert a10["fits"]["serve_fp16_b"] == int(usable / 2)
    assert a10["fits"]["serve_4bit_b"] == int(usable / 0.55)
    assert a10["fits"]["lora_bf16_b"] == int(24 / 7)
    assert a10["fits"]["qlora_4bit_b"] == int(24 / 3)
    assert "not promises" in guide["fits_basis"]


def test_multi_gpu_rungs_state_the_one_box_truth():
    """The honesty caveat from the README must ride on every multi-GPU
    rung: one machine, tensor parallel for serving, and clusters are NOT
    distributed training."""
    guide = gpu_guide.build_guide(catalog())
    b200 = next(r for r in guide["rungs"]
                if r["instance_type"] == "gpu_8x_b200_sxm6")
    assert "8 GPUs in ONE machine" in b200["note"]
    assert "tensor_parallel=8" in b200["note"]
    assert "not distributed training" in b200["note"]


def test_a_card_this_file_never_met_still_gets_true_numbers():
    """A new provider card must degrade to provider data + arithmetic,
    never be dropped and never inherit another family's prose."""
    guide = gpu_guide.build_guide(catalog(gpu_1x_z999={
        "description": "1x Z999 (500 GB NVL)",
        "gpu_description": "Z999 (500 GB NVL)",
        "price_usd_per_hour": 9.99,
        "specs": {"vcpus": 64, "memory_gib": 1024, "storage_gib": 4000,
                  "gpus": 1},
        "regions_with_capacity": ["us-east-1"],
    }))
    z = next(r for r in guide["rungs"] if r["instance_type"] == "gpu_1x_z999")
    assert z["vram_total_gib"] == 500                  # parsed, not curated
    assert z["price_usd_per_hour"] == 9.99
    assert "No curated notes" in z["good_for"]
    assert z["fits"]["serve_fp16_b"] == int(500 * 0.8 / 2)


def test_unparseable_vram_is_null_not_guessed():
    guide = gpu_guide.build_guide(catalog(gpu_1x_mystery={
        "description": "1x Mystery",
        "gpu_description": "Mystery Card",
        "price_usd_per_hour": 5.00,
        "specs": {"vcpus": 8, "memory_gib": 64, "storage_gib": 500,
                  "gpus": 1},
        "regions_with_capacity": [],
    }))
    m = next(r for r in guide["rungs"] if r["instance_type"] == "gpu_1x_mystery")
    assert m["vram_total_gib"] is None
    assert m["fits"] is None
    assert m["price_per_gib_hour"] is None
    assert m["price_usd_per_hour"] == 5.00             # price still true


def test_known_families_carry_their_own_notes():
    """A10 and B200 - the two ends of the ladder the launch post talks
    about - must each get family prose, not the generic fallback."""
    guide = gpu_guide.build_guide(catalog())
    a10 = next(r for r in guide["rungs"] if r["family"] == "A10")
    b200 = next(r for r in guide["rungs"] if r["family"] == "B200")
    assert "workhorse" in a10["good_for"]
    assert a10["era"].startswith("Ampere")
    assert "frontier" in b200["good_for"]
    assert b200["era"].startswith("Blackwell")


# -- the route ----------------------------------------------------------------


def test_route_serves_the_guide_from_the_live_catalog(client):
    resp = client.get("/gpu-guide")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fits_basis"] == gpu_guide.FITS_BASIS
    assert len(body["rungs"]) >= 1
    # Every rung's price must appear verbatim in /instance-types - one
    # catalog, one price, two screens that cannot disagree.
    types = client.get("/instance-types").json()
    for rung in body["rungs"]:
        assert rung["price_usd_per_hour"] == \
            types[rung["instance_type"]]["price_usd_per_hour"]
