-- Persist the tenant CT's IP, VMID, and public subdomain on the sites row.
-- Apply once after 003_widen_sites_status.sql.
--
-- subdomain is the host label under brieblast.tld (e.g. "acme" → acme.brieblast.tld)
-- and is the key the Caddy gateway uses to look up the upstream IP at request time.
-- See CLOUDFLARE_GATEWAY_SETUP.md.

ALTER TABLE public.sites
  ADD COLUMN IF NOT EXISTS ip_address text,
  ADD COLUMN IF NOT EXISTS vmid integer,
  ADD COLUMN IF NOT EXISTS subdomain text;

-- Subdomains must be globally unique across all users (DNS is a flat namespace).
-- Partial index so multiple rows can sit at NULL while a site is still uploading.
CREATE UNIQUE INDEX IF NOT EXISTS sites_subdomain_key
  ON public.sites (subdomain)
  WHERE subdomain IS NOT NULL;
