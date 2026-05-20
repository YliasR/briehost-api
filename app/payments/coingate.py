"""CoinGate provider stub. Phase 0: registers itself, raises NotImplementedError.
Phase 5 fills these in.

Implementation notes for Phase 5:
- POST {COINGATE_API_BASE}/orders with header `Authorization: Token {api_key}`.
- Returns a hosted `payment_url` to redirect the user to. CoinGate handles the
  multi-coin UX on their side, settles to EUR.
- Webhook signature is HMAC-SHA256 over the raw body using COINGATE_WEBHOOK_SECRET.
  Header is `X-CoinGate-Signature`. Compare with `hmac.compare_digest`.
- Event statuses: 'paid' → succeeded, 'invalid'/'expired'/'canceled' → failed/cancelled.
- Sandbox has a "simulate payment" button on every invoice; no need to move
  actual testnet coin to test the success path.
"""
from __future__ import annotations

from app.payments import (
    CheckoutSession,
    ProviderNotConfiguredError,
    WebhookResult,
    register,
)


def _require_configured(settings) -> None:
    if not settings.coingate_api_key:
        raise ProviderNotConfiguredError("CoinGate is not configured (COINGATE_API_KEY missing)")


def create_intent(settings, user_id: str, plan_id: str) -> CheckoutSession:
    _require_configured(settings)
    raise NotImplementedError("CoinGate create_intent — Phase 5")


def handle_webhook(settings, headers: dict[str, str], raw_body: bytes) -> WebhookResult:
    _require_configured(settings)
    raise NotImplementedError("CoinGate handle_webhook — Phase 5")


class _Adapter:
    create_intent = staticmethod(create_intent)
    handle_webhook = staticmethod(handle_webhook)


register("coingate", _Adapter())
