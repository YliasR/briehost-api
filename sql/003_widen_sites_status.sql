-- Widen sites.status to cover the scanning states written by app/worker.py.
-- Apply once after 002_create_sites.sql (which lives in the brieblast-landing repo).

ALTER TABLE public.sites
  DROP CONSTRAINT IF EXISTS sites_status_check;

ALTER TABLE public.sites
  ADD CONSTRAINT sites_status_check
  CHECK (status IN ('uploaded', 'scanning', 'scan_failed',
                    'provisioning', 'live', 'failed'));
