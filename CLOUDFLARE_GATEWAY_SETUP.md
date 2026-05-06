# Public subdomains via Cloudflare Tunnel + Caddy gateway

Each tenant site gets a public URL like `https://<slug>.brieblast.tld` without
exposing Proxmox to the internet. One shared gateway CT does all ingress; tenant
CTs stay on the private bridge with no public ports.

## How it works at runtime

```
                 Cloudflare edge
                       │   (TLS terminated here, wildcard cert from CF)
                       ▼
              cloudflared (gateway CT)
                       │   (single Tunnel, ingress: *.brieblast.tld → 127.0.0.1:80)
                       ▼
                  Caddy (gateway CT)
                       │   (host-header routing, upstream looked up dynamically)
              ┌────────┼────────┐
              ▼        ▼        ▼
         tenant CT  tenant CT  tenant CT
         10.0.0.5   10.0.0.6   10.0.0.7
         (Apache)   (Apache)   (Apache)
```

1. User requests `https://acme.brieblast.tld`.
2. Cloudflare resolves the wildcard CNAME to the tunnel and forwards the
   request through `cloudflared`. No public IP, no open ports on OPNsense.
3. `cloudflared` hands the request to Caddy on `127.0.0.1:80`.
4. Caddy reads the `Host` header (`acme.brieblast.tld`), pulls the matching
   `slug → ip_address` row from Supabase, and reverse-proxies to that tenant CT.
5. Tenant CT serves the PHP site over plain HTTP on the private bridge.

The `sites` table is the single source of truth for routing. No per-site Caddy
file, no Cloudflare API call per provision, no gateway reload.

## What you need to set up in Proxmox

One-time work. The playbooks assume all of this is in place.

### A. Gateway CT

Create a new LXC on the same node, attached to the **same tenant bridge** the
provisioned CTs use (e.g. `vmbr1`).

| Setting     | Value                                              |
|-------------|----------------------------------------------------|
| Hostname    | `brie-gw`                                          |
| Distro      | Debian 12 (matches the tenant template)            |
| Cores / RAM | 1 core, 512 MB is plenty                           |
| Disk        | 4 GB                                               |
| Network     | bridge `vmbr1`, **static IP** (e.g. `10.0.0.2/24`) |
| Features    | `nesting=1` (lets cloudflared run cleanly)         |

Static IP matters: Caddy and `cloudflared` only listen on `127.0.0.1`, but
tenant CTs route to the gateway by the tenant bridge, so its address must not
move. Reserve the IP in OPNsense (or leave it outside the DHCP pool).

### B. OPNsense

No port forwards. The gateway CT initiates the tunnel outbound; nothing on the
WAN side needs opening. Confirm only that the gateway CT can reach the
internet (HTTPS to `*.cloudflareclient.com` and `*.argotunnel.com`).

### C. DNS for tenant CTs

Tenant CTs already get DHCP leases on `vmbr1`. The worker stores the lease IP
in `sites.ip_address` (per `sql/004_sites_ip_vmid.sql`). Caddy reads this row
on every request, so no extra DNS plumbing is needed inside the tenant network.

## What you need to set up in Cloudflare

### A. Domain

Either `brieblast.tld` is already on Cloudflare, or add it now (Cloudflare's
"Add site" flow, then point the registrar's nameservers at CF).

### B. Tunnel

In **Zero Trust → Networks → Tunnels**:

1. Create a tunnel called `brie-gw`. Pick "Cloudflared" as the connector.
2. Cloudflare shows an installer command with a token — copy it. The Ansible
   role (when we add it) will install `cloudflared` on the gateway CT and
   register it with that token.
3. Add a **Public Hostname** entry on the tunnel:
   - Subdomain: `*`
   - Domain: `brieblast.tld`
   - Service: `HTTP`, URL: `localhost:80`
4. Save. Cloudflare auto-creates the wildcard CNAME `*.brieblast.tld → <tunnel-id>.cfargotunnel.com` (proxied, orange cloud).

### C. Reserve the apex (optional)

If you want `brieblast.tld` itself to keep pointing at the marketing site,
leave the existing `@` A/AAAA record alone. The wildcard only matches
subdomains.

## Where things live

| Component        | Host         | Notes                                                |
|------------------|--------------|------------------------------------------------------|
| API + worker     | API CT       | unchanged — still writes `sites.ip_address` on live  |
| `cloudflared`    | gateway CT   | one tunnel, ingress `*.brieblast.tld → 127.0.0.1:80` |
| Caddy            | gateway CT   | dynamic upstream lookup against Supabase             |
| Apache + site    | tenant CT    | unchanged                                            |
| Routing truth    | Supabase     | `sites.subdomain → sites.ip_address`                 |

## Schema addition you'll want

To support a stable subdomain that survives a re-provision (the slug is
already on the row, but it's not guaranteed unique across users):

```sql
ALTER TABLE public.sites
  ADD COLUMN IF NOT EXISTS subdomain text UNIQUE;
```

The worker fills this in alongside `ip_address`/`vmid` on the live transition.
Caddy queries `select ip_address from sites where subdomain = $1 and status = 'live'`.

## What still needs to be built

This doc is just the manual prerequisites. Code changes to follow:

- Ansible role `gateway_setup` — installs `cloudflared` + Caddy on the gateway
  CT, drops the Caddyfile with the dynamic-upstream module, registers the
  tunnel from a token in `.env`.
- Caddy module / sidecar for the Supabase lookup (Caddy's `http.handlers.subroute`
  + a small `exec`/`forward_auth` shim, or a custom plugin — TBD).
- Worker change: write `subdomain` on the live transition and surface it in
  the `/sites` API response so the dashboard can show the public URL.
