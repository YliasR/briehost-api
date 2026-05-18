"""The dark-pattern skip-payment 'provider'.

Treated as a payment provider so every plan activation — paid or skipped —
flows through the same audited code path (write payment_intents row, flip
profiles.plan). See docs/PAYMENTS.md for the ethical framing.

Implementation note: unlike the real providers, skip has no checkout and
no webhook. `create_intent` is the entire flow: it writes a payment_intents
row with status='succeeded' and amount_cents=0, then returns a checkout_url
that the frontend treats as "already done, go to return page."

The actual plan flip happens in the route layer (`app/routes/payments.py`)
on the `POST /api/payments/skip` endpoint, which uses this provider via
the same dispatch as the others.
"""
from __future__ import annotations

from app.payments import (
    CheckoutSession,
    WebhookResult,
    register,
)


def create_intent(settings, user_id: str, plan_id: str) -> CheckoutSession:
    # Phase 3 fills this in — for now, raise so /api/payments/skip 503s if
    # called before the dark-pattern UX work lands.
    raise NotImplementedError("Skip create_intent — Phase 3 (dark-pattern UX)")


def handle_webhook(settings, headers: dict[str, str], raw_body: bytes) -> WebhookResult:
    # Skip has no webhook — no external provider to call us back.
    raise NotImplementedError("Skip has no webhook; this should never be called")


class _Adapter:
    create_intent = staticmethod(create_intent)
    handle_webhook = staticmethod(handle_webhook)


register("skip", _Adapter())
