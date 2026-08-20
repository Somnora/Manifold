"""The curated GCP shelf: which machines Manifold offers, and what they cost.

Phase 87. GCE has no "GPU instance types with prices" list the way Lambda
does: GPUs arrive three different ways (fixed-GPU families like g2/a2/a3,
attachable accelerators on n1, and TPUs - a different API entirely), and
prices live in a separate Billing Catalog service. So Manifold curates a
shelf: a dozen machine shapes that cover the ladder from a $0.54/hr T4 to
an 8x H100 node, each mapped to the accelerator the live API can confirm
per zone.

WHAT IS LIVE AND WHAT IS A LABEL. Zone availability is live: the provider
intersects this shelf with `acceleratorTypes.aggregatedList` at request
time, so a zone only appears if Google says the accelerator exists there.
Prices are NOT live in v1: they are Google's published on-demand list
prices for us-central1, recorded on the date below, and every catalog
entry carries `price_basis` saying exactly that. The money rule (exact or
labelled) is satisfied by the label - and the spend ledger stays honest
because launch rows record this rate, which the bill may not match. The
Billing Catalog API is the v2 upgrade.

Pure data + pure functions: no SDK imports, no I/O, testable without
credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Stamped into every entry's price_basis. Update it when refreshing prices.
PRICES_RECORDED = "2026-08-14"

# The region every price below is quoted in. Travels to the client as its
# own field (price_basis_region) so the launch form can compare it with the
# selected zone instead of parsing the prose sentence beneath it.
PRICE_BASIS_REGION = "us-central1"

PRICE_BASIS = (
    f"Google's on-demand list price for {PRICE_BASIS_REGION} as recorded "
    f"{PRICES_RECORDED}, not a live meter. Your bill is Google's; spot, "
    f"sustained-use and regional differences all move it."
)

# The GCE accelerator-type ids the live availability check keys on.
L4 = "nvidia-l4"
T4 = "nvidia-tesla-t4"
A100_40 = "nvidia-tesla-a100"
A100_80 = "nvidia-a100-80gb"
H100_80 = "nvidia-h100-80gb"


@dataclass(frozen=True)
class GCPShelfEntry:
    """One machine shape Manifold will launch on GCE."""

    name: str                 # catalog name; synthetic for n1+accelerator
    machine_type: str         # the GCE machine type actually requested
    accelerator: str          # accelerator-type id (availability + attach)
    accelerator_count: int
    # Zero means the GPUs are built into the machine type (g2/a2/a3) and
    # must NOT be passed as guestAccelerators; nonzero means an n1 attach.
    attach_count: int
    gpu_type: str             # display name, matched by the hardware guide
    gpu_ram_gb: int
    vcpus: int
    ram_gb: int
    price_cents_per_hour: int  # dated list price; see PRICE_BASIS
    note: str = ""


SHELF: tuple[GCPShelfEntry, ...] = (
    GCPShelfEntry("n1-standard-4-t4", "n1-standard-4", T4, 1, 1,
                  "T4", 16, 4, 15, 54,
                  "machine + attached T4, priced together"),
    GCPShelfEntry("n1-standard-8-t4", "n1-standard-8", T4, 1, 1,
                  "T4", 16, 8, 30, 73,
                  "machine + attached T4, priced together"),
    GCPShelfEntry("g2-standard-4", "g2-standard-4", L4, 1, 0,
                  "L4", 24, 4, 16, 71),
    GCPShelfEntry("g2-standard-8", "g2-standard-8", L4, 1, 0,
                  "L4", 24, 8, 32, 85),
    GCPShelfEntry("g2-standard-24", "g2-standard-24", L4, 2, 0,
                  "L4", 24, 24, 96, 200),
    GCPShelfEntry("g2-standard-48", "g2-standard-48", L4, 4, 0,
                  "L4", 24, 48, 192, 400),
    GCPShelfEntry("a2-highgpu-1g", "a2-highgpu-1g", A100_40, 1, 0,
                  "A100", 40, 12, 85, 367),
    GCPShelfEntry("a2-highgpu-4g", "a2-highgpu-4g", A100_40, 4, 0,
                  "A100", 40, 48, 340, 1468),
    GCPShelfEntry("a2-highgpu-8g", "a2-highgpu-8g", A100_40, 8, 0,
                  "A100", 40, 96, 680, 2936),
    GCPShelfEntry("a2-ultragpu-1g", "a2-ultragpu-1g", A100_80, 1, 0,
                  "A100", 80, 12, 170, 507),
    GCPShelfEntry("a2-ultragpu-8g", "a2-ultragpu-8g", A100_80, 8, 0,
                  "A100", 80, 96, 1360, 4055),
    GCPShelfEntry("a3-highgpu-8g", "a3-highgpu-8g", H100_80, 8, 0,
                  "H100", 80, 208, 1872, 8800,
                  "often needs a reservation; on-demand capacity is rare"),
)

BY_NAME: dict[str, GCPShelfEntry] = {e.name: e for e in SHELF}


def gpu_description(entry: GCPShelfEntry) -> str:
    """Display string, shaped to match the hardware guide's family
    patterns ("A100 (40 GB ...)" etc.), so GCP rungs pick up the same
    curated ladder notes as their Lambda twins."""
    return f"{entry.gpu_type} ({entry.gpu_ram_gb} GB)"


def zones_for(entry: GCPShelfEntry,
              accelerator_zones: dict[str, list[str]]) -> list[str]:
    """The zones this shelf entry is offered in, from LIVE data.

    `accelerator_zones` maps accelerator-type id -> zones where Google's
    aggregatedList says it exists. An entry whose accelerator appears
    nowhere gets an empty list - shown as out of capacity, never guessed.
    """
    return sorted(accelerator_zones.get(entry.accelerator, []))


def quota_metric(entry: GCPShelfEntry) -> str:
    """The per-region quota metric that gates this entry.

    GCE quota metrics are per accelerator family (NVIDIA_L4_GPUS etc.).
    A fresh project holds ZERO of all of them - which is the single most
    common reason a first GCP launch fails, so the launch form shows this
    number before the click.
    """
    return {
        L4: "NVIDIA_L4_GPUS",
        T4: "NVIDIA_T4_GPUS",
        A100_40: "NVIDIA_A100_GPUS",
        A100_80: "NVIDIA_A100_80GB_GPUS",
        H100_80: "NVIDIA_H100_GPUS",
    }[entry.accelerator]


def zone_to_region(zone: str) -> str:
    """us-central1-a -> us-central1. Pure string math; a zone is always
    region + '-' + letter.

    A value that is already a region comes back untouched. Blind rsplit
    turned "us-central1" into "us", which then travelled as the scope label
    on quota rows - a place name Google never uses.
    """
    head, _, tail = zone.rpartition("-")
    if head and len(tail) == 1 and tail.isalpha():
        return head
    return zone
