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

Manual install, top-to-bottom on the gateway CT (`brie-gw`). The Ansible role
that automates this hasn't been written yet — follow these steps for the first
gateway and we can codify them later.

All commands run as `root` inside the CT.

### 1. Base packages

```bash
apt-get update
apt-get install -y curl ca-certificates debian-keyring debian-archive-keyring \
                   apt-transport-https gnupg
```

### 2. cloudflared

Cloudflare publishes its own apt repo. Don't use the static `.deb` from a blog
post — it won't auto-update.

```bash
mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null

echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared bookworm main" \
  > /etc/apt/sources.list.d/cloudflared.list

apt-get update
apt-get install -y cloudflared
```

Register the tunnel using the token Cloudflare gave you when you created the
`brie-gw` tunnel (the `eyJ...` blob, not the tunnel ID):

```bash
cloudflared service install <TOKEN>
systemctl status cloudflared       # should be active (running)
```

This installs a systemd unit, stores the token under `/etc/cloudflared/`, and
starts the tunnel. In the Cloudflare dashboard the tunnel should flip to
**Healthy** within a few seconds.

### 3. Caddy

We need a Caddy build that can do **dynamic upstream selection** — i.e. pick
the upstream per request based on the `Host` header. The standard Debian
package can do this with the built-in `dynamic_upstreams` of the `reverse_proxy`
directive plus a small lookup. Install the official build first:

```bash
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg

curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  > /etc/apt/sources.list.d/caddy-stable.list

apt-get update
apt-get install -y caddy
```

Verify:

```bash
caddy version          # v2.7+ ships dynamic_upstreams
systemctl status caddy
```

### 4. Caddyfile

Drop this at `/etc/caddy/Caddyfile`. The lookup helper at `/__upstream` is a
tiny PostgREST/Supabase call wrapped by `caddy` itself — it asks the API CT
for the IP behind a given subdomain.

```caddy
{
    # No automatic HTTPS — TLS is terminated at the Cloudflare edge and the
    # tunnel hands us plain HTTP on 127.0.0.1:80.
    auto_https off
    admin off
}

:80 {
    @hasSubdomain header_regexp host Host ^(?P<slug>[a-z0-9-]+)\.brieblast\.tld$

    handle @hasSubdomain {
        reverse_proxy {
            dynamic a {
                # Resolve <slug>.tenants.brie.internal via the lookup service
                # below; tenant CTs are not in public DNS, this is a private zone.
                name {re.host.slug}.tenants.brie.internal
                refresh 30s
            }
            header_up Host {http.request.host}
        }
    }

    handle {
        respond "brieblast gateway: unknown host" 404
    }
}
```

> The "lookup service" piece is intentionally hand-wavy — it's the one bit we
> still need to decide between (a) a custom Caddy plugin doing a direct
> Supabase REST call, or (b) a tiny FastAPI route on the API CT that returns
> `<ip>` for a slug and gets fronted by the API's existing auth. Either fits
> on top of this Caddyfile without changing it. **TODO before going live.**

Reload after edits:

```bash
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

### 5. Smoke test

From the gateway CT itself, simulating what cloudflared sends:

```bash
curl -H 'Host: <existing-slug>.brieblast.tld' http://127.0.0.1/
```

You should get the tenant site's index page. Then from anywhere on the public
internet:

```bash
curl -I https://<existing-slug>.brieblast.tld/
```

Cloudflare's `cf-ray` header in the response confirms the request went
edge → tunnel → Caddy → tenant.

## What still needs to be built

This doc covers the manual prerequisites and one-time install. Code changes
to follow:

- Ansible role `gateway_setup` — codifies steps 1–4 above so the gateway CT
  can be rebuilt from scratch.
- Lookup mechanism for Caddy's `dynamic_upstreams` (Caddy plugin vs. FastAPI
  shim) — see the TODO in the Caddyfile section.
- Worker change: assign and persist `sites.subdomain` on the live transition
  (defaulting to the slug, with a uniqueness retry), and surface it in the
  `/sites` API response so the dashboard can show the public URL.
