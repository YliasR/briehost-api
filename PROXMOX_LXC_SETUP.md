# Proxmox LXC recommendation for `briehost-api`

## Manual prerequisites (do these before the API can provision anything)

The API CT can do nothing on Proxmox without these in place. Each one is a one-time setup; the playbooks assume them.

### A. Tenant network behind OPNsense

- A dedicated bridge for tenant CTs (e.g. `vmbr1`), with its only "wire" being the OPNsense LAN-side interface.
- OPNsense has **DHCP enabled** on that interface, leasing from a pool large enough for your max concurrent sites.
- (Eventual public exposure) port-forward `:80`/`:443` from OPNsense WAN → a frontend reverse-proxy CT (Caddy/Nginx) on the same `vmbr1`. Tenant CTs are *not* exposed directly.
- Confirm: a manual test CT on `vmbr1` gets a DHCP lease and can `curl https://example.com`.

### B. Proxmox API token for briehost

In the Proxmox UI:

1. **Datacenter → Permissions → Users**: create user `briehost@pve` (or reuse `root@pam` for the lab; not recommended for prod).
2. **Datacenter → Permissions → API Tokens**: create token `briehost` for that user, **uncheck "Privilege Separation"** for the lab (or grant matching ACLs to the token explicitly).
3. **Datacenter → Permissions**: add ACL on path `/pool/briehost` for that user/token with role `PVEVMAdmin` (covers Allocate, Clone, Config, PowerMgmt, Audit). Add `PVEDatastoreUser` on `/storage/local-lvm` so it can clone disks.
4. Put the token id and secret into the API CT's `.env` as `PROXMOX_TOKEN_ID` / `PROXMOX_TOKEN_SECRET`.

### C. SSH from the API CT to the Proxmox host

The token alone can't `pct push` files. The playbooks SSH into the Proxmox host as root.

```bash
# on the API CT
ssh-keygen -t ed25519 -N '' -f /root/.ssh/id_ed25519
ssh-copy-id root@<proxmox-host>
```

On the Proxmox host, lock the key down in `/root/.ssh/authorized_keys`:

```
from="<api-ct-ip>",no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA... briehost-api
```

Smoke test from the API CT:

```bash
ansible -i /opt/briehost-api/infra/ansible/inventory/production.ini proxmox -m ping
```

### D. Tenant resource pool

```text
Datacenter → Permissions → Pools → Create → "briehost"
```

The token's ACL from step B is scoped to this pool, so all tenant CTs land in it and the token can't touch anything else.

### E. PHP "golden" template CT

The tenant CTs are full clones of this. Build it once, snapshot, convert to template:

1. Create an unprivileged Debian 12 CT, hostname `tpl-php`, on `vmbr1`, **8 GB thin-provisioned disk** on `local-lvm`, 1 vCPU / 512 MB.
2. Inside it:
   ```bash
   apt update
   apt install -y nginx php-fpm php-cli php-mbstring php-xml php-curl php-zip unzip
   rm -f /etc/nginx/sites-enabled/default
   ```
3. Drop a minimal vhost at `/etc/nginx/sites-available/site.conf`:
   ```nginx
   server {
       listen 80 default_server;
       server_name _;
       root /var/www/html;
       index index.php index.html;
       location / { try_files $uri $uri/ /index.php?$query_string; }
       location ~ \.php$ {
           include snippets/fastcgi-php.conf;
           fastcgi_pass unix:/run/php/php-fpm.sock;
       }
   }
   ```
   ```bash
   ln -s /etc/nginx/sites-available/site.conf /etc/nginx/sites-enabled/site.conf
   systemctl enable --now nginx php*-fpm
   chown -R www-data:www-data /var/www/html
   ```
4. Install the in-CT deploy script at `/usr/local/bin/deploy-site.sh` (the `deploy_site_zip` ansible role calls it after `pct push`):

   ```bash
   cat >/usr/local/bin/deploy-site.sh <<'EOF'
   #!/bin/bash
   # Usage: deploy-site.sh /path/to/file.zip
   set -euo pipefail

   ZIP_FILE="${1:-}"
   WEB_ROOT="/var/www/html"

   if [[ -z "$ZIP_FILE" ]]; then
       echo "No zip file provided." >&2
       exit 1
   fi
   if [[ ! -r "$ZIP_FILE" ]]; then
       echo "Zip file not readable: $ZIP_FILE" >&2
       exit 1
   fi

   rm -rf -- "${WEB_ROOT:?}"/* "${WEB_ROOT:?}"/.[!.]* 2>/dev/null || true
   unzip -qq -o "$ZIP_FILE" -d "$WEB_ROOT"
   chown -R www-data:www-data "$WEB_ROOT"
   find "$WEB_ROOT" -type d -exec chmod 755 {} +
   find "$WEB_ROOT" -type f -exec chmod 644 {} +

   echo "Deployment OK"
   EOF
   chmod 0755 /usr/local/bin/deploy-site.sh
   ```

5. Stop the CT, then **right-click → Convert to template** (or `pct template <vmid>` from the host).
6. Note the VMID. Put it in the API CT's `.env` as `PHP_TEMPLATE_VMID`.

### F. Variables to set in `infra/ansible/inventory/group_vars/proxmox.yml`

Most defaults work; the ones you'll likely change:

- `tenant_bridge` → must match the OPNsense-facing bridge from step A (default `vmbr1`).
- `tenant_disk_storage` → storage id where tenant disks live (default `local-lvm`, thin-provisioned).
- `tenant_disk_gb` → bumped to `8` per your call. Raise here if customers need more.

### G. Public hostnames (for later, when sites go public)

Out of scope for the first cut — when you're ready:

- Wildcard DNS `*.briehost.tld → <opnsense WAN IP>`.
- Frontend reverse-proxy CT (Caddy with `on_demand_tls` is the lowest-effort) that the `healthcheck` role writes vhost config into and reloads.
- The role currently only HTTP-checks the tenant IP; extending it to also push proxy config is the next step once DNS/TLS is decided.

---

## Recommended container profile (efficient + production-safe)

Use an **unprivileged Debian 12 LXC** (minimal template) for this API service.

| Setting | Recommended value | Why |
| --- | --- | --- |
| OS template | `debian-12-standard` | Stable, lightweight, long support |
| Container type | Unprivileged | Better isolation and safer defaults |
| vCPU | 2 | Enough for FastAPI + background provisioning calls |
| RAM | 2048 MB | Comfortable for Python + upload/extract workflow |
| Swap | 512 MB | Avoid OOM spikes during unzip/provision bursts |
| Root disk | 64+ GB | OS + app + logs + uploaded zips (kept inside CT) |
| Data mount | None | All data stays inside container root disk |
| Network | Static IP on `vmbr0` | Predictable API endpoint for dashboard/proxy |
| Start at boot | Enabled | Service recovers after host reboot |

> If this stays low traffic, you can start with **1 vCPU / 1 GB RAM**, then scale up to the values above when provisioning load increases.

## Perfect setup tutorial

## 1) Create the LXC in Proxmox

1. Download template: **Node -> local -> CT Templates -> Templates -> debian-12-standard**.
2. **Create CT**:
   - Unprivileged container: **checked**
   - Hostname: `briehost-api`
   - Password or SSH key: set securely
3. Resources:
   - Cores: `2`
   - Memory: `2048`
   - Swap: `512`
   - Root disk: `64G` minimum (more if you expect many uploads)
4. Network:
   - Bridge: `vmbr0`
   - IPv4: static (recommended), set gateway
5. DNS: your resolver(s)
6. Advanced:
   - Start at boot: **enabled**
   - On boot order: place after network-critical services if needed

If the CT already exists with a smaller disk, you can increase disk size from the Proxmox web UI (**CT -> Resources -> Root Disk -> Resize**), no host shell required.

## 2) Keep storage fully inside the container

Since you do not have Proxmox host shell access, keep uploads on the CT root disk and size it accordingly.

```bash
mkdir -p /var/brieblast/clients
```

Uploads land here as `<user_id>/<slug>-<site_id>.zip` — slug is derived from the original filename so listings stay human-readable, the UUID guarantees uniqueness.

## 3) Base packages inside container

```bash
apt update
apt install -y python3 python3-venv python3-pip curl git \
               ansible \
               clamav clamav-daemon
```

`ansible` runs the provisioning playbooks. `clamav-daemon` provides `clamd`, used by the upload scanner. Debian commonly starts it with a Unix socket at `/run/clamav/clamd.ctl`, which this app can use directly.

Initial ClamAV setup:

```bash
systemctl stop clamav-freshclam
freshclam                       # one-shot signature pull
systemctl enable --now clamav-freshclam   # background updates
systemctl enable --now clamav-daemon
ss -lxnp | grep clamd           # confirm the Unix socket is present
ls -l /run/clamav/clamd.ctl
```

If you prefer TCP instead, keep the existing `TCPSocket 3310` / `TCPAddr 127.0.0.1` settings and set `CLAMD_SOCKET=` empty in the API env to force TCP.

```conf
TCPSocket 3310
TCPAddr 127.0.0.1
```

Then `systemctl restart clamav-daemon`.

## 4) Deploy app

```bash
mkdir -p /opt/briehost-api
cd /opt/briehost-api
git clone <your-repo-url> .

python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

## 5) Apply Supabase schema

The SQL migrations live in the dashboard repo (`brieblast-landing/supabase/migrations/`):

- `001_create_profiles.sql`
- `002_create_sites.sql`

Apply both via the Supabase SQL editor or `supabase db push`. After 002 is applied, run the API-repo migration that **widens the `sites.status` CHECK constraint** to cover the scanning states the worker now writes:

```bash
# from the briehost-api repo
psql "$SUPABASE_DB_URL" -f sql/003_widen_sites_status.sql
# or paste sql/003_widen_sites_status.sql into the Supabase SQL editor
```

`error_message` is already in the schema; the worker writes scan/ansible failure tails there.

## 6) Configure environment

Create `/opt/briehost-api/.env` (see `.env.example` for the full list):

```env
SUPABASE_URL=...
SUPABASE_SERVICE_ROLE_KEY=...
SUPABASE_JWT_AUDIENCE=authenticated

PROXMOX_HOST=...
PROXMOX_USER=...
PROXMOX_TOKEN_ID=...
PROXMOX_TOKEN_SECRET=...
PROXMOX_NODE=pve
PHP_TEMPLATE_VMID=...

STORAGE_ROOT=/var/brieblast/clients
MAX_UPLOAD_BYTES=104857600

# Provisioning
PROVISIONER_BACKEND=ansible
ANSIBLE_PLAYBOOK_PATH=/opt/briehost-api/infra/ansible/playbooks/provision_site.yml
ANSIBLE_INVENTORY_PATH=/opt/briehost-api/infra/ansible/inventory/production.ini

# Malware scan + zip policy
ENABLE_MALWARE_SCAN=true
CLAMD_HOST=127.0.0.1
CLAMD_PORT=3310
MAX_ZIP_FILES=5000
MAX_ZIP_UNCOMPRESSED_BYTES=524288000
MAX_ZIP_COMPRESSION_RATIO=200

ALLOWED_ORIGINS=https://your-dashboard-domain
API_HOST=0.0.0.0
API_PORT=80
```

Use absolute paths for `ANSIBLE_PLAYBOOK_PATH` / `ANSIBLE_INVENTORY_PATH` since the systemd unit's working directory may differ from where you run things by hand.

## 7) Wire up Ansible against Proxmox

Edit `/opt/briehost-api/infra/ansible/inventory/production.ini` so the `[proxmox]` host points at your real node, with an SSH user that can run `pct` (typically `root`). Drop the matching SSH key into the CT and authorize it on the Proxmox host:

```bash
ssh-keygen -t ed25519 -N '' -f /root/.ssh/id_ed25519
ssh-copy-id root@<proxmox-host>
ansible -i /opt/briehost-api/infra/ansible/inventory/production.ini proxmox -m ping
```

Install the Proxmox collection on the API CT once:

```bash
ansible-galaxy collection install community.general
```

The roles under `infra/ansible/roles/{proxmox_cleanup,proxmox_clone,start_container,deploy_site_zip,healthcheck}` are wired up:

- `proxmox_cleanup` — destroys any tenant CT left behind by a prior failed run for the same `site_id` (matches on the `description: briehost site_id=<uuid>` tag we set on every CT we create — never touches anything else).
- `proxmox_clone` — picks a free VMID, full-clones the PHP template into the `briehost` pool, configures cores/memory/swap and a DHCP NIC on `tenant_bridge`, grows rootfs to `tenant_disk_gb`.
- `start_container` — starts the CT, waits for `pct exec` to work, polls until OPNsense hands out a DHCP lease, records `tenant_ip`.
- `deploy_site_zip` — `pct push`es the validated zip into the tenant CT, unzips into `/var/www/html`, fixes ownership, reloads php-fpm.
- `healthcheck` — `uri` GET against `tenant_ip` until it returns < 500.

Defaults (per-tenant: 1 vCPU / 512 MB / 8 GB thin disk, bridge `vmbr1`, storage `local-lvm`, pool `briehost`) live in `infra/ansible/inventory/group_vars/proxmox.yml`.

## 8) Run as a systemd service

Create `/etc/systemd/system/briehost-api.service`:

```ini
[Unit]
Description=briehost-api (FastAPI)
After=network.target clamav-daemon.service
Wants=clamav-daemon.service

[Service]
Type=simple
WorkingDirectory=/opt/briehost-api
EnvironmentFile=/opt/briehost-api/.env
ExecStart=/opt/briehost-api/.venv/bin/python -m app
Restart=always
RestartSec=3
User=root
Group=root

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
systemctl daemon-reload
systemctl enable --now briehost-api
```

## 9) Optional but recommended: reverse proxy + TLS

Put Nginx/Caddy in front of the API port and expose only 80/443 externally.
Keep container firewall open only for required ports.

## 10) Health check

```bash
curl http://127.0.0.1/healthz
```

Expected response:

```json
{"status":"ok"}
```

End-to-end smoke test (uploads a tiny zip, watches the row transition):

```bash
echo '<?php echo "hi"; ?>' > index.php && zip /tmp/smoke.zip index.php
curl -H "Authorization: Bearer <supabase-jwt>" \
     -F file=@/tmp/smoke.zip \
     http://127.0.0.1/api/sites/upload
# then watch status in Supabase: uploaded -> scanning -> provisioning -> live
```

## Notes specific to this repository

- Provisioning is now Ansible-driven (`app/worker.py` + `infra/ansible/`). The old `app/proxmox.py` is a deprecation marker — see `ANSIBLE_PROVISIONING_APPROACH.md` for the design.
- Upload and storage paths are designed for LXC usage via `STORAGE_ROOT`; stored filenames are `<slug>-<site_id>.zip` for human-readable directory listings.
- `ENABLE_MALWARE_SCAN=true` fails closed: if `clamd` is unreachable, uploads land in `scan_failed` rather than slipping through.
