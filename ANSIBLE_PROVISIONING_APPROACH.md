# Ansible provisioning approach

This document describes how to switch provisioning from in-process Python/Proxmox calls to an Ansible-driven workflow, while keeping this API as the upload/auth/status control plane.

## Goals

1. Keep `POST /api/sites/upload` fast and non-blocking.
2. Move provisioning logic into versioned Ansible playbooks/roles.
3. Keep status updates in Supabase (`sites.status`) as the source of truth for the dashboard.
4. Add a malware scanning gate before extraction/provisioning.

## Proposed flow

1. Dashboard uploads `file` (`.zip`) to `/api/sites/upload`.
2. API stores zip at `STORAGE_ROOT/<user_id>/<site_id>.zip` and inserts `sites` row (`status = uploaded`).
3. API enqueues a background provisioning job (Celery/RQ/Arq/worker thread, depending on deployment).
4. Worker sets status to `scanning`, runs malware checks on the uploaded zip.
5. If scan passes, worker sets status to `provisioning` and runs `ansible-playbook`.
6. On success: worker sets status to `live` and stores runtime metadata (VMID/IP/domain if available).
7. On failure: worker sets status to `failed` and stores a concise error reason.

## Ansible layout (recommended)

```text
infra/
  ansible/
    inventory/
      production.ini
    playbooks/
      provision_site.yml
    roles/
      proxmox_clone/
      deploy_site_zip/
      start_container/
      healthcheck/
```

`provision_site.yml` should accept vars like:

- `site_id`
- `user_id`
- `zip_path`
- `target_node`
- `template_vmid`
- `site_name` (optional display only)

## API changes in this repo

1. Keep current upload logic in `app/routes/sites.py`.
2. Replace the TODO with enqueue call (job id can be stored in `sites` if useful).
3. Add a worker module that:
   - loads site metadata
   - executes scan
   - executes `ansible-playbook`
   - writes status transitions and failures back to Supabase.
4. Keep `app/proxmox.py` either:
   - removed, or
   - turned into a thin adapter that shells out to Ansible.

## Running playbooks from worker

Example command shape:

```bash
ansible-playbook infra/ansible/playbooks/provision_site.yml \
  -i infra/ansible/inventory/production.ini \
  -e "site_id=<site_id> user_id=<user_id> zip_path=<absolute_zip_path> target_node=<node> template_vmid=<vmid>"
```

Implementation notes:

- Use `subprocess.run(..., check=False, capture_output=True, text=True)`.
- Treat non-zero exit code as provisioning failure.
- Persist stderr/stdout summary (trimmed) for observability.
- Avoid logging secrets/tokens from env.

## Malware checking for uploaded zips

Yes, this can and should be implemented.

### Practical baseline

1. **ClamAV scan (required baseline)**
   - Scan the uploaded zip before extraction/provisioning.
   - Prefer daemon mode (`clamd`) for performance.
   - Default to the Unix socket at `/run/clamav/clamd.ctl` via `CLAMD_SOCKET`; if you prefer TCP, keep `CLAMD_HOST` / `CLAMD_PORT` and configure `clamd` with `TCPSocket 3310` and `TCPAddr 127.0.0.1`.
2. **Zip policy checks (already partly present)**
   - Keep traversal/symlink protections (`app/storage.py`).
   - Add limits for:
     - max file count
     - max uncompressed size
     - max compression ratio (zip-bomb defense).
3. **Optional YARA rules**
   - Add signature/rule checks for suspicious PHP/webshell patterns.

### Status model with scanning

Suggested statuses:

- `uploaded`
- `scanning`
- `scan_failed`
- `provisioning`
- `live`
- `failed`

If malware is detected, move to `scan_failed` and do not run Ansible.

### Worker pseudo-sequence

```text
uploaded -> scanning
if scan fails: scanning -> scan_failed
if scan passes: scanning -> provisioning
if ansible rc == 0: provisioning -> live
else: provisioning -> failed
```

## New environment variables (suggested)

```env
PROVISIONER_BACKEND=ansible
ANSIBLE_PLAYBOOK_PATH=infra/ansible/playbooks/provision_site.yml
ANSIBLE_INVENTORY_PATH=infra/ansible/inventory/production.ini
ANSIBLE_EXTRA_VARS_JSON={}
ENABLE_MALWARE_SCAN=true
CLAMD_HOST=127.0.0.1
CLAMD_PORT=3310
MAX_ZIP_FILES=5000
MAX_ZIP_UNCOMPRESSED_BYTES=524288000
MAX_ZIP_COMPRESSION_RATIO=200
```

## Operational notes

- Run worker separately from API process.
- Keep idempotency: retries should not corrupt state.
- Ensure failed runs are diagnosable without exposing secrets.
- Keep storage path ID-based even with user-friendly display names.
