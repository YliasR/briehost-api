"""Plan-limit enforcement for site uploads.

Single entry point: `assert_can_create_site(user_id, new_bytes)`. Raises a
402 Payment Required HTTPException when the user would exceed their plan's
site-count or storage cap. The 402 is intentional — the frontend keys off
the status code to show an "upgrade your plan" CTA rather than a generic
"something went wrong" toast.

Two checks happen here, each round-trips Supabase once:
1. Site count vs `plan.max_sites`
2. Total storage (current + new upload) vs `plan.max_storage_bytes`

Both are skipped for plans where the corresponding limit is `None`
("unlimited"). Admin trivially passes everything.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, status

from app.db import admin_client
from app.plans import PlanLimits, get_plan_limits

log = logging.getLogger("briehost.limits")


def get_user_plan(user_id: str) -> str | None:
    """Look up `profiles.plan` for a user. Returns None if no row found
    (which `get_plan_limits` will treat as the most-restrictive default)."""
    rows = (
        admin_client()
        .table("profiles")
        .select("plan")
        .eq("id", user_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        return None
    return rows[0].get("plan")


def get_user_plan_limits(user_id: str) -> PlanLimits:
    """Convenience: plan-id lookup + limits lookup in one call. Use this
    when you need to do staged checks (count first, storage later after
    you know the upload size) and want to avoid two profile reads."""
    return get_plan_limits(get_user_plan(user_id))


def _count_user_sites(user_id: str) -> int:
    """Number of sites currently owned by the user. Counts every row in
    `sites` for them — `failed` and `scan_failed` rows still consume the
    user's quota until they delete them, by design (otherwise you could
    spam-upload garbage zips to bypass count limits)."""
    res = (
        admin_client()
        .table("sites")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .execute()
    )
    return res.count or 0


def _sum_user_storage(user_id: str) -> int:
    """Sum of `size_bytes` across the user's sites. Same rationale as
    above — failed uploads still count until deleted."""
    rows = (
        admin_client()
        .table("sites")
        .select("size_bytes")
        .eq("user_id", user_id)
        .execute()
        .data
        or []
    )
    return sum((r.get("size_bytes") or 0) for r in rows)


def _fmt_gb(bytes_value: int) -> str:
    return f"{bytes_value / (1024 ** 3):.2f} GB"


def assert_within_site_count(user_id: str, limits: PlanLimits) -> None:
    """Cheap upfront check — does the user have room for one more site?
    Skipped if plan has unlimited sites."""
    if limits.max_sites is None:
        return
    current = _count_user_sites(user_id)
    if current >= limits.max_sites:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "reason": "plan_site_limit",
                "message": (
                    f"Your plan allows {limits.max_sites} site"
                    f"{'s' if limits.max_sites != 1 else ''}; you already have {current}. "
                    "Delete a site or upgrade your plan."
                ),
                "limit": limits.max_sites,
                "current": current,
            },
        )


def assert_within_storage(user_id: str, limits: PlanLimits, new_bytes: int) -> None:
    """Storage check including the prospective new upload. Skipped if plan
    has unlimited storage."""
    if limits.max_storage_bytes is None:
        return
    current = _sum_user_storage(user_id)
    if current + new_bytes > limits.max_storage_bytes:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "reason": "plan_storage_limit",
                "message": (
                    f"This upload ({_fmt_gb(new_bytes)}) would put you at "
                    f"{_fmt_gb(current + new_bytes)}, over your plan's "
                    f"{_fmt_gb(limits.max_storage_bytes)} cap. "
                    "Delete a site or upgrade your plan."
                ),
                "limit_bytes": limits.max_storage_bytes,
                "current_bytes": current,
                "new_bytes": new_bytes,
            },
        )


def assert_can_create_site(user_id: str, new_bytes: int) -> None:
    """Single entry point for the upload routes. Site count is checked first
    (cheap, no need to read the upload at all if you're already at the cap);
    storage is checked second with the prospective new size folded in."""
    plan = get_user_plan(user_id)
    limits = get_plan_limits(plan)
    log.debug("limit-check user=%s plan=%s limits=%s new_bytes=%s", user_id, plan, limits, new_bytes)
    assert_within_site_count(user_id, limits)
    assert_within_storage(user_id, limits, new_bytes)
