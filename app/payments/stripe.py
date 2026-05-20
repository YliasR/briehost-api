"""Stripe provider — cards in Phase 1. Bancontact slot-in in Phase 2 by
just appending 'bancontact' to PAYMENT_METHOD_TYPES.

Flow:
1. `create_intent` builds a hosted Checkout Session. Stripe returns a URL;
   user is redirected, pays on stripe.com, gets bounced back to
   `{PAYMENTS_RETURN_BASE_URL}/payment-return/{intent_id}`.
2. Stripe POSTs to `/api/payments/stripe/webhook` with a signed body. We
   verify via `stripe.Webhook.construct_event`, then on
   `checkout.session.completed` return the WebhookResult so the route
   layer flips the user's plan.

Idempotency: the unique constraint on (provider, provider_ref) in
payment_intents + the route layer's status check before flipping plan
means duplicate webhooks (Stripe retries on any 5xx or timeout) are safe.
"""
from __future__ import annotations

import logging
import uuid

import stripe as stripe_sdk

# Stripe SDK v10+ moved error classes to the top level. Old `stripe.error.*`
# paths still work as a back-compat shim in v10/11 but were removed in v12+.
# Import the two we use directly so we don't care which version is installed.
try:
    from stripe import SignatureVerificationError, StripeError  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover — very old SDK
    from stripe.error import SignatureVerificationError, StripeError  # type: ignore[no-redef]

from app.payments import (
    CheckoutSession,
    PLAN_DISPLAY_NAMES,
    ProviderNotConfiguredError,
    WebhookResult,
    WebhookVerificationError,
    get_plan_price,
    register,
)

log = logging.getLogger("briehost.payments.stripe")

# Phase 2: card + bancontact. Stripe Checkout shows both as radio options
# on its hosted page; the user picks per-transaction. Both are single-use,
# EUR-only (which we already are), so no extra branching needed in our code.
#
# Bancontact is async — the user authorizes in their bank app, control returns
# to Stripe, and only then does the session flip to paid. For most successful
# payments the `checkout.session.completed` webhook fires with
# `payment_status='paid'` and we're done. If Stripe needs longer (rare), we
# get `completed` with `payment_status='unpaid'` first, then either
# `async_payment_succeeded` or `async_payment_failed` later. Both are handled
# in handle_webhook().
PAYMENT_METHOD_TYPES = ["card", "bancontact"]


def _require_configured(settings) -> None:
    if not settings.stripe_secret_key:
        raise ProviderNotConfiguredError("Stripe is not configured (STRIPE_SECRET_KEY missing)")


def _configure_sdk(settings) -> None:
    """Set the SDK key on every call rather than once at import — keeps
    things stateless and makes it trivial to swap keys in tests."""
    stripe_sdk.api_key = settings.stripe_secret_key


def create_intent(settings, user_id: str, plan_id: str) -> CheckoutSession:
    _require_configured(settings)
    _configure_sdk(settings)

    amount_cents = get_plan_price(plan_id)
    intent_id = str(uuid.uuid4())
    return_base = settings.payments_return_base_url.rstrip("/")

    try:
        session = stripe_sdk.checkout.Session.create(
            mode="payment",
            payment_method_types=PAYMENT_METHOD_TYPES,
            line_items=[
                {
                    "price_data": {
                        "currency": "eur",
                        "product_data": {
                            "name": f"BrieHosting — {PLAN_DISPLAY_NAMES.get(plan_id, plan_id)}",
                            # Stripe shows description on the checkout page.
                            "description": "One-time payment to activate this plan (school demo, no recurring charge).",
                        },
                        "unit_amount": amount_cents,
                    },
                    "quantity": 1,
                }
            ],
            # success_url MUST include {CHECKOUT_SESSION_ID} or our own intent id
            # so the return page can poll for status. We use our intent_id (uuid
            # we generated above) — easier than parsing Stripe's session id back
            # to our row, and the metadata carries everything else we need.
            success_url=f"{return_base}/payment-return/{intent_id}?status=success",
            cancel_url=f"{return_base}/payment-return/{intent_id}?status=cancel",
            # Metadata is echoed back on the webhook payload — this is how we
            # tie a webhook event to our payment_intents row + the user's plan.
            metadata={
                "intent_id": intent_id,
                "user_id": user_id,
                "plan_id": plan_id,
            },
            # When `payment_intent_data.metadata` is set, the underlying
            # PaymentIntent also carries the metadata. Belt-and-braces in
            # case we ever switch to listening for `payment_intent.succeeded`.
            payment_intent_data={
                "metadata": {
                    "intent_id": intent_id,
                    "user_id": user_id,
                    "plan_id": plan_id,
                },
            },
        )
    except StripeError as exc:
        log.exception("Stripe Checkout Session create failed for user=%s plan=%s", user_id, plan_id)
        user_msg = getattr(exc, "user_message", None) or str(exc)
        raise RuntimeError(f"Stripe rejected the request: {user_msg}") from exc

    return CheckoutSession(
        intent_id=intent_id,
        provider="stripe",
        checkout_url=session.url,
        provider_ref=session.id,
        amount_cents=amount_cents,
        currency="EUR",
    )


def handle_webhook(settings, headers: dict[str, str], raw_body: bytes) -> WebhookResult:
    _require_configured(settings)
    if not settings.stripe_webhook_secret:
        # Webhook secret is separate from the secret key — Stripe gives it
        # to you only after you register the endpoint in the dashboard.
        raise ProviderNotConfiguredError(
            "Stripe webhook is not configured (STRIPE_WEBHOOK_SECRET missing)"
        )
    _configure_sdk(settings)

    sig_header = headers.get("stripe-signature", "")
    if not sig_header:
        raise WebhookVerificationError("missing Stripe-Signature header")

    try:
        event = stripe_sdk.Webhook.construct_event(
            payload=raw_body,
            sig_header=sig_header,
            secret=settings.stripe_webhook_secret,
        )
    except SignatureVerificationError as exc:
        raise WebhookVerificationError(str(exc)) from exc
    except ValueError as exc:
        # Malformed payload (not valid JSON, etc.). Treat as a verification
        # failure so the route returns 400 instead of 5xx.
        raise WebhookVerificationError(f"invalid payload: {exc}") from exc

    # Stripe SDK v15+ removed StripeObject's dict inheritance AND
    # to_dict_recursive(). The supported way to get a plain nested dict is
    # `.to_dict()` (which now recurses automatically). See:
    # https://github.com/stripe/stripe-python/wiki/Migration-guide-for-v15
    event_dict = event.to_dict()
    event_type = event_dict["type"]
    data = event_dict["data"]["object"]
    metadata = data.get("metadata") or {}

    # Event matrix:
    #   completed + paid          → succeeded (cards, instant Bancontact)
    #   completed + unpaid        → still pending (async Bancontact, wait for next event)
    #   async_payment_succeeded   → succeeded (late-arriving Bancontact settlement)
    #   async_payment_failed      → failed
    #   expired                   → cancelled
    #   anything else             → ack as pending, log
    if event_type == "checkout.session.completed":
        if data.get("payment_status") == "paid":
            return WebhookResult(
                intent_id=metadata.get("intent_id", ""),
                provider="stripe",
                provider_ref=data["id"],
                status="succeeded",
                paid_plan_id=metadata.get("plan_id"),
            )
        # Async-payment-pending. Row stays 'pending' until async_payment_*
        # event lands. Don't touch the plan yet.
        return WebhookResult(
            intent_id=metadata.get("intent_id", ""),
            provider="stripe",
            provider_ref=data["id"],
            status="pending",
            paid_plan_id=None,
        )

    if event_type == "checkout.session.async_payment_succeeded":
        # Late settlement of an async method (typically Bancontact). The
        # plan flip happens here, NOT on the earlier `completed` event.
        return WebhookResult(
            intent_id=metadata.get("intent_id", ""),
            provider="stripe",
            provider_ref=data["id"],
            status="succeeded",
            paid_plan_id=metadata.get("plan_id"),
        )

    if event_type in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        status = "cancelled" if event_type == "checkout.session.expired" else "failed"
        return WebhookResult(
            intent_id=metadata.get("intent_id", ""),
            provider="stripe",
            provider_ref=data["id"],
            status=status,
            paid_plan_id=None,
        )

    # Unknown event — return a no-op pending so the route ACKs with 200.
    # Stripe will stop retrying. Logged so we notice if we're missing an
    # event type we should be handling.
    log.info("ignoring Stripe event type=%s id=%s", event_type, event_dict.get("id"))
    return WebhookResult(
        intent_id="",
        provider="stripe",
        provider_ref=data.get("id", ""),
        status="pending",
        paid_plan_id=None,
    )


class _Adapter:
    create_intent = staticmethod(create_intent)
    handle_webhook = staticmethod(handle_webhook)


register("stripe", _Adapter())
