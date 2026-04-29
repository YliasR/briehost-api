# briehost-api

Upload + Proxmox LXC provisioning API for the BrieHosting dashboard.

The dashboard (sibling repo `brieblast-landing`) sends a zipped PHP site here. This service:

1. Verifies the Supabase JWT supplied by the dashboard.
2. Streams the upload to disk under `STORAGE_ROOT/<user_id>/<site_id>.zip`.
3. Records a row in the Supabase `sites` table.
4. (Background) Clones a pre-baked PHP LXC template via the Proxmox API, extracts the site into it, starts the container, and updates `sites.status`.

## Local development

```bash
# create venv (uv recommended)
uv venv
source .venv/bin/activate   # or `.venv\Scripts\activate` on Windows
uv pip install -e .

# env
cp .env.example .env
# fill in SUPABASE_*, PROXMOX_*, etc.

# run
API_PORT=8000 python -m app
```

Point the dashboard at `VITE_BRIEHOST_API_URL=http://localhost:8000`.

## Deployment

Runs in its own Proxmox LXC. The production container already has the env populated — `.env.example` is for local dev only.

For an Ansible-based provisioning model (including zip malware scanning gates), see `ANSIBLE_PROVISIONING_APPROACH.md`. When uploads don't reach `live`, walk `TROUBLESHOOTING.md`. For a web UI over every ansible run, see `docs/ara.md`.

## API

### `POST /api/sites/upload`

`multipart/form-data` with field `file` (a `.zip`, ≤ 100 MB).

Headers: `Authorization: Bearer <supabase-access-token>`.

Response `200`:
```json
{ "siteId": "uuid", "status": "uploaded" }
```

Provisioning continues asynchronously; the dashboard polls `sites.status` via Supabase.

## Security notes

- Zips are extracted with absolute-path / `..` / symlink rejection (see `app/storage.py`).
- JWT verification uses Supabase JWKS (cached); no shared secret in the repo.
- Service-role key must never leave the server.
