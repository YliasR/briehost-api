"""Payment endpoints.

Thin dispatch layer: the actual provider integration lives in
`app/payments/<provider>.py`. This module only:
- validates inputs / authenticates the caller
- looks up the right provider via `app.payments.get_provider`
- writes the payment_intents row + flips profiles.plan on success

See docs/PAYMENTS.md for the full architecture.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from app.auth import current_user_id
from app.config import Settings, get_settings
from app.db import admin_client
from app.payments import (
    PROVIDERS,
    ProviderNotConfiguredError,
    WebhookVerificationError,
    get_provider,
)

log = logging.getLogger("briehost.routes.payments")

router = APIRouter(prefix="/api/payments", tags=["payments"])


# --- helpers --------------------------------------------------------------


def _insert_intent_row(
    intent_id: str,
    user_id: str,
    plan_id: str,
    provider: str,
    provider_ref: str,
    amount_cents: int,
    currency: str = "EUR",
    status_value: str = "pending",
) -> None:
    """Write the row that maps our intent_id ↔ provider's reference. Done
    before the user is redirected so the webhook has something to update
    even if the provider beats the redirect (race that does happen)."""
    admin_client().table("payment_intents").insert(
        {
            "id": intent_id,
            "user_id": user_id,
            "plan_id": plan_id,
            "provider": provider,
            "provider_ref": provider_ref,
            "amount_cents": amount_cents,
            "currency": currency,
            "status": status_value,
        }
    ).execute()


def _flip_plan(user_id: str, plan_id: str) -> None:
    """Activate a plan on the user's profile. Called from both the webhook
    handlers (real payments) and the skip endpoint (dark pattern). Single
    code path means a single audit log later if we want one."""
    admin_client().table("profiles").update({"plan": plan_id}).eq("id", user_id).execute()


def _update_intent_status(provider_ref: str, provider: str, status_value: str) -> str | None:
    """Mark an intent succeeded/failed/cancelled by provider+ref. Returns
    the intent's `user_id` so the caller can flip the plan; None if no row
    matched (out-of-band webhook, replay, etc.)."""
    res = (
        admin_client()
        .table("payment_intents")
        .update({"status": status_value})
        .eq("provider", provider)
        .eq("provider_ref", provider_ref)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None
    return rows[0].get("user_id")


# --- create intent --------------------------------------------------------


@router.post("/intents")
async def create_intent(
    payload: dict = Body(...),
    user_id: str = Depends(current_user_id),
    settings: Settings = Depends(get_settings),
):
    plan_id = (payload.get("planId") or payload.get("plan_id") or "").strip()
    provider_name = (payload.get("provider") or "").strip().lower()
    if not plan_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "planId is required")
    if provider_name not in PROVIDERS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"provider must be one of: {sorted(PROVIDERS)}",
        )

    try:
        provider = get_provider(provider_name)
        session = provider.create_intent(settings, user_id, plan_id)
    except ProviderNotConfiguredError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except NotImplementedError as exc:
        # Phase 0 reality: stubs raise this. Return a clear 501 so the
        # frontend can show a "coming soon" state for providers that aren't
        # wired up yet, instead of a confusing 500.
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc)) from exc

    _insert_intent_row(
        intent_id=session.intent_id,
        user_id=user_id,
        plan_id=plan_id,
        provider=session.provider,
        provider_ref=session.provider_ref,
        amount_cents=session.amount_cents,
        currency=session.currency,
    )

    return {
        "intentId": session.intent_id,
        "checkoutUrl": session.checkout_url,
        "provider": session.provider,
    }


@router.get("/intents/{intent_id}")
async def get_intent(
    intent_id: str,
    user_id: str = Depends(current_user_id),
):
    """Polled by the return page while the webhook lands. RLS would normally
    handle ownership, but we belt-and-brace here with .eq('user_id', ...)."""
    rows = (
        admin_client()
        .table("payment_intents")
        .select("id, plan_id, provider, status, amount_cents, currency, created_at")
        .eq("id", intent_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "intent not found")
    return rows[0]


# --- skip-payment (dark pattern) -----------------------------------------


@router.post("/skip")
async def skip_payment(
    payload: dict = Body(...),
    user_id: str = Depends(current_user_id),
    settings: Settings = Depends(get_settings),
):
    """Activates the requested plan with no payment. See docs/PAYMENTS.md
    for the framing — this is a deliberate, audited dark-pattern path that
    we'll write up in the report."""
    plan_id = (payload.get("planId") or payload.get("plan_id") or "").strip()
    if not plan_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "planId is required")

    try:
        provider = get_provider("skip")
        session = provider.create_intent(settings, user_id, plan_id)
    except NotImplementedError as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc)) from exc

    _insert_intent_row(
        intent_id=session.intent_id,
        user_id=user_id,
        plan_id=plan_id,
        provider="skip",
        provider_ref=session.provider_ref,
        amount_cents=0,
        status_value="succeeded",
    )
    _flip_plan(user_id, plan_id)
    return {"plan": plan_id, "intentId": session.intent_id}


# --- webhooks -------------------------------------------------------------
#
# All three webhook routes share the same shape:
#   1. Read raw body (signature verification needs the exact bytes).
#   2. Dispatch to the provider's handle_webhook.
#   3. Update row + flip plan on success.
#
# Wrapped in a helper to keep the per-provider routes one-liners.


async def _handle_webhook(request: Request, provider_name: str, settings: Settings):
    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        provider = get_provider(provider_name)
        result = provider.handle_webhook(settings, headers, raw_body)
    except WebhookVerificationError as exc:
        # 400 (not 5xx) so the provider doesn't retry forever on a real
        # signature mismatch (= someone replaying or a misconfig we should
        # see in logs).
        log.warning("%s webhook signature verification failed: %s", provider_name, exc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid signature") from exc
    except ProviderNotConfiguredError as exc:
        log.error("%s webhook called but provider not configured: %s", provider_name, exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except NotImplementedError as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc)) from exc
    except Exception:
        # Last-resort guard: anything we don't recognize is logged with full
        # traceback so we can debug from `journalctl` instead of staring at a
        # generic 500 in Stripe's delivery log.
        log.exception("%s webhook handler crashed", provider_name)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "webhook handler crashed; see API logs",
        )

    try:
        user_id = _update_intent_status(result.provider_ref, provider_name, result.status)
        if result.status == "succeeded" and user_id and result.paid_plan_id:
            _flip_plan(user_id, result.paid_plan_id)
    except Exception:
        log.exception(
            "%s webhook DB write failed for provider_ref=%s status=%s",
            provider_name,
            result.provider_ref,
            result.status,
        )
        # Return 500 so Stripe retries — better than ACKing with 200 and
        # losing the event if the DB blip clears.
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "webhook DB write failed; will retry",
        )

    log.info(
        "%s webhook handled: provider_ref=%s status=%s plan=%s user=%s",
        provider_name,
        result.provider_ref,
        result.status,
        result.paid_plan_id,
        user_id if result.status == "succeeded" else None,
    )
    return {"ok": True}


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request, settings: Settings = Depends(get_settings)):
    return await _handle_webhook(request, "stripe", settings)


@router.post("/paypal/webhook")
async def paypal_webhook(request: Request, settings: Settings = Depends(get_settings)):
    return await _handle_webhook(request, "paypal", settings)


@router.post("/coingate/webhook")
async def coingate_webhook(request: Request, settings: Settings = Depends(get_settings)):
    return await _handle_webhook(request, "coingate", settings)
