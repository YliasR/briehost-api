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

## 3) Base packages inside container

```bash
apt update
apt install -y python3 python3-venv python3-pip curl git
```

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

## 5) Configure environment

Create `/opt/briehost-api/.env`:

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
ALLOWED_ORIGINS=https://your-dashboard-domain
API_HOST=0.0.0.0
API_PORT=80
```

## 6) Run as a systemd service

Create `/etc/systemd/system/briehost-api.service`:

```ini
[Unit]
Description=briehost-api (FastAPI)
After=network.target

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

## 7) Optional but recommended: reverse proxy + TLS

Put Nginx/Caddy in front of the API port and expose only 80/443 externally.  
Keep container firewall open only for required ports.

## 8) Health check

```bash
curl http://127.0.0.1/healthz
```

Expected response:

```json
{"status":"ok"}
```

## Notes specific to this repository

- This repo currently has a provisioning stub in `app/proxmox.py` (`NotImplementedError`), so clone/start behavior will not work until that logic is implemented.
- Upload and storage paths are already designed for LXC usage via `STORAGE_ROOT`.
