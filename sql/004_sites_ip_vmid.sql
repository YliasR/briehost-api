-- Persist the tenant CT's IP and VMID on the sites row so the dashboard can link to it.
-- Apply once after 003_widen_sites_status.sql.

ALTER TABLE public.sites
  ADD COLUMN IF NOT EXISTS ip_address text,
  ADD COLUMN IF NOT EXISTS vmid integer;
