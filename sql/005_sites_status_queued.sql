-- Add 'queued' to sites.status so the provisioner can show concurrent
-- uploads waiting their turn instead of pretending work is happening.
-- Apply once after 004_sites_ip_vmid.sql.

ALTER TABLE public.sites
  DROP CONSTRAINT IF EXISTS sites_status_check;

ALTER TABLE public.sites
  ADD CONSTRAINT sites_status_check
  CHECK (status IN ('uploaded', 'queued', 'scanning', 'scan_failed',
                    'provisioning', 'live', 'failed'));
