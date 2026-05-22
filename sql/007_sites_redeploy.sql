-- Track the git repo a site was uploaded from so a one-click redeploy can
-- re-pull without making the user re-enter the URL + branch. Also add the
-- 'updating' status and a last_deploy_at column so the UI can show "updated
-- 5 minutes ago" and enforce a rate limit on rapid re-clicks.
-- Apply once after 006_payment_intents.sql.

ALTER TABLE public.sites
  ADD COLUMN IF NOT EXISTS repo_host text,
  ADD COLUMN IF NOT EXISTS repo_url text,
  ADD COLUMN IF NOT EXISTS repo_branch text,
  ADD COLUMN IF NOT EXISTS last_deploy_at timestamptz;

ALTER TABLE public.sites
  DROP CONSTRAINT IF EXISTS sites_status_check;

ALTER TABLE public.sites
  ADD CONSTRAINT sites_status_check
  CHECK (status IN ('uploaded', 'queued', 'scanning', 'scan_failed',
                    'provisioning', 'updating', 'live', 'failed'));
