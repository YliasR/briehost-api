-- payment_intents: one audit row per checkout attempt, across every provider.
--
-- See docs/PAYMENTS.md for the architecture this slots into.
--
-- Numbering note: the migration numbering is shared with the brieblast-landing
-- repo (001, 002, 005, 006, 007 live there; 003, 004 here). Order of apply:
--   001_create_profiles.sql            (brieblast-landing)
--   002_create_sites.sql               (brieblast-landing)
--   003_widen_sites_status.sql         (this repo)
--   004_sites_ip_vmid.sql              (this repo)
--   003_add_admin_plan.sql ...
--     ... through 007_add_email_to_profiles.sql  (brieblast-landing)
--   005_payment_intents.sql            (this file)
--
-- Depends on objects created in earlier migrations:
--   - public.profiles                     (001_create_profiles.sql)
--   - auth.users                          (Supabase built-in)
--   - public.update_updated_at()          (001_create_profiles.sql)
--   - public.is_admin()                   (006_final_admin_rls_fix.sql, SQL+SECURITY DEFINER)
--
-- Plan IDs match the paid subset of the CHECK constraint on profiles.plan.
-- If src/lib/plans.ts in brieblast-landing grows a new paid tier, this CHECK
-- *and* profiles_plan_check both need to update.
--
-- Provider 'skip' is the dark-pattern free-grant path documented in
-- docs/PAYMENTS.md — that route writes amount_cents=0 and a server-generated
-- UUID into provider_ref, succeeds immediately, no external call.

CREATE TABLE IF NOT EXISTS public.payment_intents (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  plan_id         text NOT NULL
                    CHECK (plan_id IN ('smol_brie', 'thicc_brie', 'mega_brie')),
  provider        text NOT NULL
                    CHECK (provider IN ('stripe', 'paypal', 'coingate', 'skip')),
  -- Provider's own identifier for the checkout / order. For 'skip' the API
  -- generates a fresh UUID so the UNIQUE constraint below stays meaningful.
  provider_ref    text NOT NULL,
  amount_cents    integer NOT NULL CHECK (amount_cents >= 0),
  currency        text NOT NULL DEFAULT 'EUR'
                    CHECK (length(currency) = 3),
  status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'succeeded', 'failed', 'cancelled')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),

  -- Webhook retries land on the same row. Without this, a flaky provider
  -- could double-flip a plan or write twin audit rows.
  CONSTRAINT payment_intents_provider_ref_key UNIQUE (provider, provider_ref)
);

CREATE INDEX IF NOT EXISTS payment_intents_user_id_idx
  ON public.payment_intents (user_id);

-- Partial index so "find stuck pending payments" stays cheap as the table
-- grows. Most rows end up in a terminal state quickly.
CREATE INDEX IF NOT EXISTS payment_intents_pending_idx
  ON public.payment_intents (created_at)
  WHERE status = 'pending';

ALTER TABLE public.payment_intents ENABLE ROW LEVEL SECURITY;

-- Mirrors the sites convention: users can read their own rows, every write
-- happens server-side via the service-role key (which bypasses RLS), so no
-- INSERT/UPDATE/DELETE policies here.
CREATE POLICY "Users can read own payment intents"
  ON public.payment_intents
  FOR SELECT
  USING (auth.uid() = user_id);

-- Admin visibility for the eventual dashboard. Uses the SECURITY DEFINER
-- helper so we don't recurse into payment_intents from inside its own policy.
CREATE POLICY "Admins can read all payment intents"
  ON public.payment_intents
  FOR SELECT
  USING (public.is_admin());

-- Reuse the timestamp trigger function defined in
-- brieblast-landing/supabase/migrations/001_create_profiles.sql
CREATE TRIGGER payment_intents_updated_at
  BEFORE UPDATE ON public.payment_intents
  FOR EACH ROW
  EXECUTE FUNCTION public.update_updated_at();
