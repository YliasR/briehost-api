"""Payment provider dispatch.

Each provider (stripe / paypal / coingate / skip) lives in its own module
and exposes the same two functions:

    create_intent(settings, user_id, plan_id) -> CheckoutSession
    handle_webhook(settings, headers, raw_body) -> WebhookResult

The route layer in `app/routes/payments.py` just looks the provider up in
`PROVIDERS` and calls the shared interface. Adding a fourth provider later
means dropping in a module + registering it here, no route changes.

See `docs/PAYMENTS.md` for the full architecture, provider rationale, and
the dark-pattern skip-payment design.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

ProviderName = Literal["stripe", "paypal", "coingate", "skip"]
PaymentStatus = Literal["pending", "succeeded", "failed", "cancelled"]


# Plan catalogue (prices, display names, limits) lives in app/plans.py so
# the payments module isn't the single owner of plan data. Re-exported here
# for backward compatibility with provider modules that import from here.
from app.plans import (  # noqa: E402, F401
    PLAN_DISPLAY_NAMES,
    PLAN_PRICES_CENTS,
    get_plan_price,
)


@dataclass(frozen=True)
class CheckoutSession:
    """Return value of `create_intent`. The frontend uses `checkout_url` to
    redirect (or, for PayPal smart-button, as the order id to pass to the SDK)
    and `intent_id` for polling the status while the webhook lands.
    """
    intent_id: str          # row id in payment_intents
    provider: ProviderName
    checkout_url: str       # hosted page (Stripe/CoinGate) or order id (PayPal)
    provider_ref: str       # PI id / order id; same value written to DB row
    amount_cents: int
    currency: str = "EUR"


@dataclass(frozen=True)
class WebhookResult:
    """Return value of `handle_webhook`. The route uses this to update the
    payment_intents row and, on `succeeded`, flip the user's plan.
    """
    intent_id: str          # row id in payment_intents (looked up via provider_ref)
    provider: ProviderName
    provider_ref: str
    status: PaymentStatus
    paid_plan_id: str | None  # plan to activate on success, None on failure/cancel


class PaymentProvider(Protocol):
    """Structural type — each provider module satisfies this."""

    def create_intent(self, settings, user_id: str, plan_id: str) -> CheckoutSession: ...

    def handle_webhook(self, settings, headers: dict[str, str], raw_body: bytes) -> WebhookResult: ...


class ProviderNotConfiguredError(RuntimeError):
    """Raised when a provider's required env vars are missing. The route
    layer turns this into a 503 so the frontend can hide that provider's
    button without blowing up the whole payments page.
    """


class WebhookVerificationError(Exception):
    """Raised when a webhook's signature doesn't verify. The route layer
    turns this into a 400 — providers will retry, so don't return 5xx.
    """


# Registry populated by each provider module on import. See provider files.
PROVIDERS: dict[str, PaymentProvider] = {}


def register(name: ProviderName, provider: PaymentProvider) -> None:
    PROVIDERS[name] = provider


def get_provider(name: str) -> PaymentProvider:
    if name not in PROVIDERS:
        raise ValueError(f"unknown payment provider: {name!r}")
    return PROVIDERS[name]


# Side-effect imports register each provider in PROVIDERS. Order doesn't
# matter; just keeps the registry populated when anything imports this module.
from app.payments import stripe as _stripe  # noqa: E402, F401
from app.payments import paypal as _paypal  # noqa: E402, F401
from app.payments import coingate as _coingate  # noqa: E402, F401
from app.payments import skip as _skip  # noqa: E402, F401
