# Payment systems — implementation plan

Status: **planning**, not built. This doc captures provider choices, the
shared architecture, and a phased build order so we don't bolt on a third
half-finished gateway later.

## Goals

- A real-feeling payment step before a paid plan activates. Sandbox-only,
  no real money moves, but the flows are end-to-end (intent → checkout →
  webhook → plan flip in Supabase).
- Three options on the checkout screen, in order of polish:
  1. **Card / Bancontact** via Stripe (Belgian jury sees Bancontact, recognises it instantly).
  2. **PayPal** (recognisable button, sandbox accounts are free).
  3. **Crypto** via CoinGate (EU-licensed, has a sandbox).
- A deliberate, documented dark-pattern "continue with free tier" link so we can
  write up the ethical framing in the report — that's worth as many points
  as the integration itself.

## Non-goals

- Real charges. We stay in test/sandbox mode for the demo.
- Recurring subscriptions. Plans flip on a one-shot payment; no Stripe Billing
  / PayPal subscriptions API. (Adding it later is straightforward — Stripe
  Subscriptions on top of the same Customer object.)
- KYC, tax handling, invoicing. Out of scope for a school demo.
- Webhooks behind authentication beyond the provider's own signature scheme.

## Provider choice

### Cards + Bancontact — Stripe

Already have an account. Stripe supports Bancontact as a first-class
[payment method](https://docs.stripe.com/payments/bancontact): EUR only,
single-use, redirect to the Bancontact app, then return to our site with
the result. Same `PaymentIntent` API as card payments, just a different
`payment_method_types`. Test cards (`4242 4242 4242 4242`) and Bancontact
test flow both work in test mode.

**Why not Mollie**: Stripe's developer experience is better and the account
is done. Mollie's Bancontact support is fine too — if Stripe gets weird
about onboarding for the Belgian entity, Mollie is the fallback.

### PayPal — official sandbox

PayPal sandbox is mature, has been around forever, and you get free
buyer + merchant test accounts from the developer dashboard. Use the
[JS SDK + Orders API v2](https://developer.paypal.com/docs/api/orders/v2/)
— smart-button on the frontend, capture + webhook on the backend. More
verbose than Stripe but the integration is well-trodden.

### Crypto — CoinGate

**Coinbase Commerce is dead** for non-US/Singapore merchants as of
[March 2026](https://help.coinbase.com/en/transitioning-from-coinbase-commerce-to-coinbase-business),
replacement Coinbase Business doesn't accept BE entities. So Coinbase is
out, full stop.

Going with **[CoinGate](https://developer.coingate.com/)** instead:

- EU-based (Lithuania), MiCA-licensed → cleanest regulatory story for a
  Belgian school project, jury will appreciate the EU compliance angle.
- Hosted (no self-hosted node), which matches the "no extra infra"
  constraint.
- Sandbox at `sandbox.coingate.com` with separate API keys and a checkout
  UI that behaves like prod.
- Settles to EUR, supports BTC / ETH / LTC / USDC + a handful of others.
- 1% fee in prod (irrelevant for the demo).

**Why not NOWPayments**: it's 0.5% in prod and has more coins, but
CoinGate's MiCA license + EU base gives the demo a stronger story.
NOWPayments is the fallback if CoinGate sandbox gives us trouble.

**Why not BTCPay Server (self-hosted)**: would be impressive, but we
already said no extra infra.

## Architecture

One shared shape for all three providers:

```
┌──────────────┐     1. create intent     ┌──────────────┐
│  Dashboard   │ ───────────────────────▶ │  briehost    │
│  (React)     │                          │  -api        │
│              │ ◀──────────────────────  │              │
│              │     checkout URL/token   │              │
└──────────────┘                          └──────────────┘
       │                                          ▲
       │ 2. redirect / hosted UI                  │ 4. webhook
       ▼                                          │    (signed)
┌──────────────┐     3. user pays         ┌──────────────┐
│  Stripe /    │ ───────────────────────▶ │  Provider    │
│  PayPal /    │                          │              │
│  CoinGate    │                          │              │
└──────────────┘                          └──────────────┘
                                                  │
                                                  │ 5. update
                                                  ▼
                                          ┌──────────────┐
                                          │   Supabase   │
                                          │  (plan flip) │
                                          └──────────────┘
```

The provider-specific code lives in `app/payments/<provider>.py`. They
all expose the same two functions:

```python
def create_intent(user_id: str, plan_id: str) -> CheckoutSession:
    """Return { checkout_url, intent_id, provider }."""

def handle_webhook(headers: dict, raw_body: bytes) -> WebhookResult:
    """Verify signature, return { intent_id, status, paid_plan_id }."""
```

The route layer (`app/routes/payments.py`) is thin: pick the provider
module by name, call the shared interface, write to the `payment_intents`
table. Adding a fourth provider later means adding a module + a webhook
route, nothing else.

## Database changes

New table:

```sql
CREATE TYPE payment_status AS ENUM ('pending', 'succeeded', 'failed', 'cancelled');
CREATE TYPE payment_provider AS ENUM ('stripe', 'paypal', 'coingate');

CREATE TABLE public.payment_intents (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  plan_id         text NOT NULL,                 -- 'starter' | 'pro' | etc, from PLANS in frontend
  provider        payment_provider NOT NULL,
  provider_ref    text NOT NULL,                 -- Stripe PI id / PayPal order id / CoinGate order id
  amount_cents    integer NOT NULL,
  currency        text NOT NULL DEFAULT 'EUR',
  status          payment_status NOT NULL DEFAULT 'pending',
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),

  UNIQUE (provider, provider_ref)
);

CREATE INDEX payment_intents_user_id_idx ON public.payment_intents(user_id);
CREATE INDEX payment_intents_status_idx ON public.payment_intents(status)
  WHERE status = 'pending';
```

`profiles.plan` keeps doing what it does today; the webhook just calls
the existing `updatePlan` path after marking the intent `succeeded`.

## Endpoints

```
POST  /api/payments/intents          { planId, provider } → { checkoutUrl, intentId }
GET   /api/payments/intents/{id}                          → { status, ... }   # for polling fallback
POST  /api/payments/stripe/webhook                        → 200 / 4xx          # signed
POST  /api/payments/paypal/webhook                        → 200 / 4xx          # signed
POST  /api/payments/coingate/webhook                      → 200 / 4xx          # signed
```

All three webhooks verify a provider signature before doing anything
(Stripe: `Stripe-Signature` HMAC, PayPal: certificate-based, CoinGate:
HMAC-SHA256 over the body with the merchant secret).

## Settings (env)

```env
# Stripe
STRIPE_SECRET_KEY=sk_test_...
STRIPE_PUBLISHABLE_KEY=pk_test_...        # exposed to frontend
STRIPE_WEBHOOK_SECRET=whsec_...

# PayPal
PAYPAL_ENV=sandbox                         # 'sandbox' | 'live'
PAYPAL_CLIENT_ID=...
PAYPAL_CLIENT_SECRET=...
PAYPAL_WEBHOOK_ID=...                      # for signature verification

# CoinGate
COINGATE_API_KEY=...
COINGATE_API_BASE=https://api-sandbox.coingate.com/v2
COINGATE_WEBHOOK_SECRET=...
```

Test cards for the demo:
- Card success: `4242 4242 4242 4242`, any future date, any CVC
- 3DS challenge: `4000 0027 6000 3184`
- Decline: `4000 0000 0000 0002`
- Bancontact: select Bancontact on the test page, click "Authorize"
- PayPal: log into the sandbox buyer account, click pay
- CoinGate: sandbox checkout has a "simulate payment" button per
  invoice — no need to actually move testnet coin

## Dark-pattern "skip" — design

This is the bit that becomes a written section in the report, framed as
a *deliberate demonstration* of UX manipulation patterns we then critique.
Implement it. Don't hide it. Talk about it.

Concrete pattern stack on the upgrade page:

1. **Pre-selected plan** — middle tier is selected by default on render.
2. **Visual hierarchy** — big yellow "Continue to checkout 🧀" button,
   small grey "continue with free tier" *link* (not a button) underneath.
3. **Loss-aversion copy** on the skip path: "Are you sure? Free tier users
   only get crumbs 🥲 — you'll lose access to [list of features]."
4. **Friction asymmetry** — paying is one click, skipping is one click +
   one modal confirm.
5. **Confirm-shaming** modal buttons: primary "Take me back" vs ghost
   "Continue with crumbs anyway".

Report angle: list each pattern, name it (it's all named in the
[deceptive.design](https://www.deceptive.design/) taxonomy), explain why
it works, explain why we'd remove it for a real shipping product, and
cite the EU [Digital Services Act](https://digital-strategy.ec.europa.eu/en/policies/digital-services-act-package)
provisions on dark patterns. Jury bait.

## Frontend changes (brieblast-landing)

- New `PaymentMethodPicker` component on the plan-upgrade flow (radio
  group: Stripe card, Bancontact, PayPal, Crypto).
- For Stripe: redirect to a hosted `checkout.stripe.com` URL (cleanest
  for sandbox demo — no need to embed Stripe Elements).
- For PayPal: render the smart-button SDK inline.
- For CoinGate: redirect to the hosted checkout invoice URL.
- New `PaymentReturnPage` route that polls `GET /api/payments/intents/{id}`
  for up to ~30s while the webhook lands, then shows success/failure +
  a button back to the dashboard.
- The dark-pattern skip lives on the plan-upgrade page above the picker.

## Implementation phases

Rough order, each phase is independently shippable:

| Phase | Scope | Estimate |
| --- | --- | --- |
| 0 | Schema migration (`sql/005_payment_intents.sql`), shared types, `app/payments/` skeleton | 0.5 day |
| 1 | Stripe cards (test mode), webhook, plan flip on success, return page | 1 day |
| 2 | Stripe Bancontact (same `PaymentIntent`, different `payment_method_types`) | 0.5 day |
| 3 | Dark-pattern UX on the plan picker + write-up draft | 0.5 day |
| 4 | PayPal sandbox (smart-button, capture, webhook) | 1 day |
| 5 | CoinGate sandbox (hosted checkout, webhook) | 1 day |
| 6 | Polish: payment-history view on the dashboard, retry on stuck webhook | 0.5 day |

Phases 1–3 are the minimum viable "we have payments." Phases 4–5 are
the "extra points" bit. Phase 6 only if there's slack.

## Open questions

- **Where does the webhook live?** The API CT sits behind the Caddy
  gateway already, so route `briehosting.be/api/payments/*/webhook` to
  the API CT in the existing Caddyfile. Provider dashboards need a
  publicly reachable URL — they will be, since the gateway is on
  Cloudflare. No tunnels needed.
- **Idempotency** on webhooks: providers retry. The unique constraint on
  `(provider, provider_ref)` plus checking `status` before flipping the
  plan handles this. Worth a unit test.
- **Sandbox key rotation**: keep them in `.env` like everything else.
  No CI / secret manager for a school demo.
- **Replay protection on the Bancontact return**: Stripe handles this
  via the `PaymentIntent` status — we never trust the redirect alone,
  only the webhook.

## Sources

- [Stripe — Bancontact payments](https://docs.stripe.com/payments/bancontact)
- [Coinbase Commerce shutdown migration guide](https://help.coinbase.com/en/transitioning-from-coinbase-commerce-to-coinbase-business)
- [CoinGate developer docs](https://developer.coingate.com/)
- [NOWPayments API + sandbox](https://documenter.getpostman.com/view/7907941/T1LSCRHC)
- [PayPal Orders v2 API](https://developer.paypal.com/docs/api/orders/v2/)
- [Deceptive design pattern catalogue](https://www.deceptive.design/)
- [EU DSA dark-pattern provisions](https://digital-strategy.ec.europa.eu/en/policies/digital-services-act-package)
