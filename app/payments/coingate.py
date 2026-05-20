"""CoinGate (crypto) provider — Phase 5.

Flow:
1. `create_intent` POSTs to {COINGATE_API_BASE}/orders. CoinGate returns
   a hosted `payment_url` (multi-coin checkout UI on their domain) and
   an `id` we store as our `provider_ref`.
2. User pays on CoinGate's page. Sandbox has a "Pay" button that simulates
   settlement without moving real coin.
3. CoinGate POSTs a form-encoded callback to
   `/api/payments/coingate/webhook` with the order status.
4. We verify the callback via a token-based signature scheme (see
   `_verification_token` below — HMAC-SHA256 of our intent_id keyed by
   COINGATE_WEBHOOK_SECRET). The token is round-tripped through CoinGate's
   `token` field, so we don't need to store anything extra to verify.

CoinGate doc reference: https://developer.coingate.com/reference/orders
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from urllib.parse import parse_qs

import httpx

from app.payments import (
    CheckoutSession,
    PLAN_DISPLAY_NAMES,
    ProviderNotConfiguredError,
    WebhookResult,
    WebhookVerificationError,
    get_plan_price,
    register,
)

log = logging.getLogger("briehost.payments.coingate")

# CoinGate order status → our PaymentStatus
# Reference: https://developer.coingate.com/docs/order-statuses
_STATUS_MAP = {
    "new": "pending",
    "pending": "pending",
    "confirming": "pending",
    "paid": "succeeded",
    "invalid": "failed",
    "expired": "cancelled",
    "canceled": "cancelled",
    "refunded": "failed",  # refunds are rare in our one-shot flow; treat as fail
}


def _require_configured(settings) -> None:
    if not settings.coingate_api_key:
        raise ProviderNotConfiguredError("CoinGate is not configured (COINGATE_API_KEY missing)")


def _verification_token(intent_id: str, webhook_secret: str) -> str:
    """Stateless signature: HMAC(intent_id, webhook_secret). Sent to CoinGate
    on order creation via the `token` field; CoinGate echoes it back in the
    webhook callback body. We re-compute and compare with hmac.compare_digest.

    This avoids needing a DB column for the token — the intent_id is already
    persisted as the lookup key, and the secret never leaves the server."""
    return hmac.new(
        webhook_secret.encode("utf-8"),
        intent_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_intent(settings, user_id: str, plan_id: str) -> CheckoutSession:
    _require_configured(settings)
    if not settings.coingate_webhook_secret:
        # We need the webhook secret at create-time too, to derive the token
        # we send with the order. Without it the callback can't be verified.
        raise ProviderNotConfiguredError(
            "CoinGate webhook is not configured (COINGATE_WEBHOOK_SECRET missing)"
        )

    amount_cents = get_plan_price(plan_id)
    amount_euros = f"{amount_cents / 100:.2f}"
    intent_id = str(uuid.uuid4())
    return_base = settings.payments_return_base_url.rstrip("/")
    token = _verification_token(intent_id, settings.coingate_webhook_secret)

    payload = {
        "order_id": intent_id,
        "price_amount": amount_euros,
        "price_currency": "EUR",
        "receive_currency": "EUR",   # settle to fiat — we don't want to manage crypto wallets
        "title": f"BrieHosting — {PLAN_DISPLAY_NAMES.get(plan_id, plan_id)}",
        "description": "One-time payment to activate this plan (school demo).",
        "callback_url": f"{settings.payments_api_base_url.rstrip('/')}/api/payments/coingate/webhook",
        "success_url": f"{return_base}/payment-return/{intent_id}?status=success",
        "cancel_url": f"{return_base}/payment-return/{intent_id}?status=cancel",
        "token": token,
        "purchaser_email": "",  # CoinGate will ask on their page
    }

    headers = {
        "Authorization": f"Token {settings.coingate_api_key}",
        "Accept": "application/json",
    }

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"{settings.coingate_api_base.rstrip('/')}/orders",
                json=payload,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        log.exception("CoinGate order create network error for user=%s plan=%s", user_id, plan_id)
        raise RuntimeError(f"CoinGate unreachable: {exc}") from exc

    if resp.status_code >= 400:
        log.error(
            "CoinGate order create rejected (status=%s body=%s)",
            resp.status_code,
            resp.text[:500],
        )
        raise RuntimeError(f"CoinGate rejected the order: HTTP {resp.status_code}")

    body = resp.json()
    payment_url = body.get("payment_url")
    coingate_id = body.get("id")
    if not payment_url or not coingate_id:
        log.error("CoinGate response missing payment_url/id: %s", body)
        raise RuntimeError("CoinGate response did not include payment_url or id")

    return CheckoutSession(
        intent_id=intent_id,
        provider="coingate",
        checkout_url=payment_url,
        # CoinGate's `id` is an integer; coerce to str for the text column.
        provider_ref=str(coingate_id),
        amount_cents=amount_cents,
        currency="EUR",
    )


def handle_webhook(settings, headers: dict[str, str], raw_body: bytes) -> WebhookResult:
    _require_configured(settings)
    if not settings.coingate_webhook_secret:
        raise ProviderNotConfiguredError(
            "CoinGate webhook is not configured (COINGATE_WEBHOOK_SECRET missing)"
        )

    # CoinGate sends application/x-www-form-urlencoded, NOT JSON.
    try:
        decoded = raw_body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WebhookVerificationError(f"invalid body encoding: {exc}") from exc

    # parse_qs returns lists; flatten by taking the first value of each key.
    fields = {k: v[0] for k, v in parse_qs(decoded, keep_blank_values=True).items()}

    coingate_id = fields.get("id", "")
    order_id = fields.get("order_id", "")  # this is our intent_id
    status = fields.get("status", "")
    received_token = fields.get("token", "")

    if not order_id or not received_token:
        raise WebhookVerificationError("missing order_id or token in callback body")

    expected_token = _verification_token(order_id, settings.coingate_webhook_secret)
    if not hmac.compare_digest(received_token, expected_token):
        raise WebhookVerificationError("token does not match expected HMAC")

    mapped_status = _STATUS_MAP.get(status, "pending")
    # We don't know the plan_id from CoinGate's payload (they don't echo our
    # metadata). The route layer looks up the existing payment_intents row by
    # (provider, provider_ref) and that row already has plan_id from create.
    # So we need _flip_plan to look up plan_id from the row instead of taking
    # it from WebhookResult. Simpler: pass intent_id through and let the route
    # use the row's plan_id. For now, we leave paid_plan_id=None on success
    # and patch the route to fall back to the row's plan_id when missing.
    return WebhookResult(
        intent_id=order_id,
        provider="coingate",
        provider_ref=str(coingate_id),
        status=mapped_status,
        paid_plan_id=None,  # route will look up from the payment_intents row
    )


class _Adapter:
    create_intent = staticmethod(create_intent)
    handle_webhook = staticmethod(handle_webhook)


register("coingate", _Adapter())
