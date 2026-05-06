# Public subdomains via Cloudflare Tunnel + Caddy gateway

Each tenant site gets a public URL like `https://<slug>.briehosting.be` without
exposing Proxmox to the internet. One shared gateway CT does all ingress; tenant
CTs stay on the private bridge with no public ports.

## How it works at runtime

```
                 Cloudflare edge
                       │   (TLS terminated here, wildcard cert from CF)
                       ▼
              cloudflared (gateway CT)
                       │   (single Tunnel, ingress: *.briehosting.be → 127.0.0.1:80)
                       ▼
                  Caddy (gateway CT)
                       │   (host-header routing, upstream looked up dynamically)
              ┌────────┼────────┐
              ▼        ▼        ▼
         tenant CT  tenant CT  tenant CT
         192.168.10.50   192.168.10.51   192.168.10.52
         (Apache)   (Apache)   (Apache)
```

1. User requests `https://acme.briehosting.be`.
2. Cloudflare resolves the wildcard CNAME to the tunnel and forwards the
   request through `cloudflared`. No public IP, no open ports on OPNsense.
3. `cloudflared` hands the request to Caddy on `127.0.0.1:80`.
4. Caddy reads the `Host` header (`acme.briehosting.be`), pulls the matching
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
| Network     | bridge `vmbr1`, **static IP** (e.g. `192.168.10.208/24`) |
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

Either `briehosting.be` is already on Cloudflare, or add it now (Cloudflare's
"Add site" flow, then point the registrar's nameservers at CF).

### B. Tunnel

In **Zero Trust → Networks → Tunnels**:

1. Create a tunnel called `brie-gw`. Pick "Cloudflared" as the connector.
2. Cloudflare shows an installer command with a token — copy it. The Ansible
   role (when we add it) will install `cloudflared` on the gateway CT and
   register it with that token.
3. Add a **Public Hostname** entry on the tunnel:
   - Subdomain: `*`
   - Domain: `briehosting.be`
   - Service: `HTTP`, URL: `localhost:80`
4. Save. Cloudflare auto-creates the wildcard CNAME `*.briehosting.be → <tunnel-id>.cfargotunnel.com` (proxied, orange cloud).

### C. Reserve the apex (optional)

If you want `briehosting.be` itself to keep pointing at the marketing site,
leave the existing `@` A/AAAA record alone. The wildcard only matches
subdomains.

## Where things live

| Component        | Host         | Notes                                                |
|------------------|--------------|------------------------------------------------------|
| API + worker     | API CT       | unchanged — still writes `sites.ip_address` on live  |
| `cloudflared`    | gateway CT   | one tunnel, ingress `*.briehosting.be → 127.0.0.1:80` |
| Caddy            | gateway CT   | dynamic upstream lookup against Supabase             |
| Apache + site    | tenant CT    | unchanged                                            |
| Routing truth    | Supabase     | `sites.subdomain → sites.ip_address`                 |

## Schema

`sql/004_sites_ip_vmid.sql` adds `ip_address`, `vmid`, and `subdomain` to
`public.sites`. Apply it to Supabase before the gateway can resolve anything:

```bash
# from the briehost-api repo
psql "$SUPABASE_DB_URL" -f sql/004_sites_ip_vmid.sql
```

`subdomain` has a partial unique index (unique when not null) — DNS is a flat
namespace, so two users can't both claim `acme`. The worker fills it in on the
live transition; Caddy looks it up on every request.

## Installing the gateway (cloudflared + Caddy)

Automated by the `gateway_setup` Ansible role. From the API CT (where you run
the other playbooks):

```bash
# 1. Reserve the gateway CT's static IP in inventory/group_vars/gateway.yml.
#    Defaults: gateway_admin_bind_ip=192.168.10.208, gateway_admin_allowed_source=192.168.10.136
#    (= the API CT's IP), gateway_domain=briehosting.be. Edit if yours differ.

# 2. Make sure the gateway is reachable over SSH as root:
ssh root@192.168.10.208 'echo ok'

# 3. Run the playbook with the Cloudflare tunnel token in the env.
#    Token = the eyJ... blob from the dashboard, NOT the tunnel UUID.
cd infra/ansible
CLOUDFLARED_TUNNEL_TOKEN=eyJ... \
  ansible-playbook -i inventory/production.ini playbooks/setup_gateway.yml
```

What the role does, in order:

1. Installs base packages (`curl`, `gnupg`, `nftables`, repo helpers).
2. Adds Cloudflare's apt repo, installs `cloudflared`, and runs
   `cloudflared service install <TOKEN>` once (gated on the systemd unit not
   already existing — re-runs are no-ops).
3. Adds Cloudsmith's Caddy apt repo and installs `caddy`.
4. Renders `/etc/caddy/Caddyfile` from the role template (admin API bound to
   `gateway_admin_bind_ip:2019`, catchall 404), validates with
   `caddy validate`, and reloads.
5. Renders `/etc/nftables.conf` so `tcp dport 2019` is dropped from any source
   that isn't `gateway_admin_allowed_source`.

Re-running the playbook is safe — it rotates the Caddyfile and nftables config
in place. The cloudflared registration is the one step that's deliberately
not re-run; if you need to re-register against a new tunnel, remove
`/etc/systemd/system/cloudflared.service` first.

### Verify

```bash
ssh root@192.168.10.208 systemctl status cloudflared caddy nftables
```

In the Cloudflare dashboard the tunnel should be **Healthy** within seconds.

### API CT configuration

Set these on the API CT's `.env` so the worker knows where to push routes:

```bash
GATEWAY_CADDY_ADMIN_URL=http://192.168.10.208:2019
GATEWAY_DOMAIN=briehosting.be
```

Restart the API service after editing.

### Smoke test

From the gateway CT itself, simulating what cloudflared sends:

```bash
curl -H 'Host: <existing-slug>.briehosting.be' http://127.0.0.1/
```

You should get the tenant site's index page. Then from anywhere on the public
internet:

```bash
curl -I https://<existing-slug>.briehosting.be/
```

Cloudflare's `cf-ray` header in the response confirms the request went
edge → tunnel → Caddy → tenant.

## What's already built

- `app/gateway.py` — Caddy admin API client (route push/remove + bulk replace
  + subdomain derivation).
- `app/worker.py` — pushes a route and persists `subdomain` on the live
  transition. Failures are logged but never flip a healthy site to `failed`.
- `app/sync_gateway.py` — bulk re-hydration. Runs automatically on API CT
  startup, and also as a CLI: `python -m app.sync_gateway`.
- `sql/004_sites_ip_vmid.sql` — adds `subdomain` with a partial unique index.

## Recovering after a gateway reboot

Caddy keeps dynamic routes in memory, so when the gateway CT reboots all
per-site routes vanish and tenants 404 until they're replayed. Two ways to
recover:

```bash
# Option A: restart the API service. The startup hook re-syncs everything.
systemctl restart briehost-api

# Option B: manual one-shot from the API CT (no API restart needed).
cd /opt/briehost-api && uv run python -m app.sync_gateway
```

Both read `status='live'` rows from Supabase and PATCH the full route set into
srv0 in one call. Idempotent — safe to run any time, even if the gateway is
already in sync.

## What still needs to be built

- Surface `subdomain` in the `/sites` API response so the dashboard can show
  the public URL.
