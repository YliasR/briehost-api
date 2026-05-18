"""PayPal provider stub. Phase 0: registers itself, raises NotImplementedError.
Phase 4 fills these in.

Implementation notes for Phase 4:
- Orders API v2: POST /v2/checkout/orders (auth via OAuth2 client credentials).
- Frontend uses the smart-button SDK with the returned order id.
- Webhook signature verification is certificate-based: GET PayPal's signing
  cert from the URL in the webhook headers, verify the body. The `paypalrestsdk`
  package handles this; alternatively call PayPal's
  /v1/notifications/verify-webhook-signature endpoint with the raw body +
  headers + PAYPAL_WEBHOOK_ID.
- Event to act on: CHECKOUT.ORDER.APPROVED → capture the order →
  PAYMENT.CAPTURE.COMPLETED is the actual settlement event.
"""
from __future__ import annotations

from app.payments import (
    CheckoutSession,
    ProviderNotConfiguredError,
    WebhookResult,
    register,
)


def _require_configured(settings) -> None:
    if not settings.paypal_client_id or not settings.paypal_client_secret:
        raise ProviderNotConfiguredError(
            "PayPal is not configured (PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET missing)"
        )


def create_intent(settings, user_id: str, plan_id: str) -> CheckoutSession:
    _require_configured(settings)
    raise NotImplementedError("PayPal create_intent — Phase 4")


def handle_webhook(settings, headers: dict[str, str], raw_body: bytes) -> WebhookResult:
    _require_configured(settings)
    raise NotImplementedError("PayPal handle_webhook — Phase 4")


class _Adapter:
    create_intent = staticmethod(create_intent)
    handle_webhook = staticmethod(handle_webhook)


register("paypal", _Adapter())
