"""Plan catalogue — single source of truth on the API side.

Mirrors brieblast-landing/src/lib/plans.ts. Keep the two in sync when
adding/changing tiers. None of these values are user-editable; the
profiles.plan column just stores the id (`smol_brie`, etc.) and we look
everything else up here.

Three things live here today:
- PLAN_PRICES_CENTS — what we charge (moved from app/payments/__init__.py)
- PLAN_DISPLAY_NAMES — pretty names for invoices / emails
- PLAN_LIMITS — what users are allowed to do per tier (enforced in app/limits.py)
"""
from __future__ import annotations

from dataclasses import dataclass

PlanId = str  # 'none' | 'smol_brie' | 'thicc_brie' | 'mega_brie' | 'admin'


@dataclass(frozen=True)
class PlanLimits:
    """A `None` value means "unlimited" — checked explicitly in app/limits.py
    rather than a sentinel like math.inf so the intent is unmistakable.

    `lxc_disk_gb` is the rootfs size the provisioner allocates to that tenant's
    Proxmox container. Thin-provisioned, so it's a ceiling not a reservation;
    sizing it to roughly match `max_storage_bytes` keeps the CT from running
    out of disk before the API quota would block the next upload. Smol stays
    at 8 GB because PHP + OS overhead won't comfortably fit in a 5 GB rootfs.
    """
    max_sites: int | None
    max_storage_bytes: int | None
    lxc_disk_gb: int


_GB = 1024 * 1024 * 1024

# Free / unconfigured users get nothing — they have to pick a plan and pay
# (or use the skip flow if we ever build it). This is the gate that gives
# the payment flow actual teeth.
PLAN_LIMITS: dict[str, PlanLimits] = {
    "none": PlanLimits(max_sites=0, max_storage_bytes=0, lxc_disk_gb=8),
    "smol_brie": PlanLimits(max_sites=1, max_storage_bytes=5 * _GB, lxc_disk_gb=8),
    "thicc_brie": PlanLimits(max_sites=None, max_storage_bytes=50 * _GB, lxc_disk_gb=50),
    "mega_brie": PlanLimits(max_sites=None, max_storage_bytes=200 * _GB, lxc_disk_gb=200),
    "admin": PlanLimits(max_sites=None, max_storage_bytes=None, lxc_disk_gb=200),
}

# Sensible fallback for any unknown / missing plan value (legacy rows,
# stale enums, etc.) — treat as 'none'. Better to over-restrict than
# accidentally give someone unlimited sites because their plan column
# was NULL.
DEFAULT_PLAN_LIMITS = PLAN_LIMITS["none"]


PLAN_PRICES_CENTS: dict[str, int] = {
    "smol_brie": 420,    # €4.20
    "thicc_brie": 1337,  # €13.37
    "mega_brie": 4269,   # €42.69
}

PLAN_DISPLAY_NAMES: dict[str, str] = {
    "smol_brie": "Smol Brie",
    "thicc_brie": "Thicc Brie",
    "mega_brie": "Mega Brie",
}


def get_plan_price(plan_id: str) -> int:
    """Returns price in cents. Raises ValueError for unknown plans."""
    if plan_id not in PLAN_PRICES_CENTS:
        raise ValueError(f"unknown plan: {plan_id!r}")
    return PLAN_PRICES_CENTS[plan_id]


def get_plan_limits(plan_id: str | None) -> PlanLimits:
    """Returns limits for a plan id, defaulting to the most-restrictive
    tier for unknown / None / missing values."""
    if not plan_id:
        return DEFAULT_PLAN_LIMITS
    return PLAN_LIMITS.get(plan_id, DEFAULT_PLAN_LIMITS)
