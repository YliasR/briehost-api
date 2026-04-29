# Proxmox LXC recommendation for `briehost-api`

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

`ansible` runs the provisioning playbooks. `clamav-daemon` provides `clamd`, used by the upload scanner.

Initial ClamAV setup:

```bash
systemctl stop clamav-freshclam
freshclam                       # one-shot signature pull
systemctl enable --now clamav-freshclam   # background updates
systemctl enable --now clamav-daemon
ss -ltnp | grep 3310            # confirm clamd is listening on TCP/3310
```

If `clamd` is bound to a unix socket only by default, edit `/etc/clamav/clamd.conf` to enable TCP:

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

The roles under `infra/ansible/roles/{proxmox_clone,deploy_site_zip,start_container,healthcheck}` are scaffolded as placeholders — fill them in with the real `community.general.proxmox` / `pct` calls before going live.

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
