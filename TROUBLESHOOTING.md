# Troubleshooting briehost uploads

When a dashboard upload doesn't end up at `status = live`, walk the pipeline in order and stop at the first stage that's wrong. The Supabase `sites` row is the source of truth for *which* stage failed; everything else is for finding *why*.

```
browser  ─►  POST /api/sites/upload  ─►  zip on disk  ─►  worker  ─►  ansible-playbook  ─►  Proxmox host  ─►  tenant CT
   1                  2                       3              4                5                    6                7
```

## 1. Did the upload reach the API?

**Browser devtools → Network tab** on `POST /api/sites/upload`:

- `200` + `{"siteId":"...","status":"uploaded"}` → API got it. Note the `siteId`, you'll grep for it everywhere else.
- `401` → JWT not being sent, expired, or wrong audience. Check `Authorization: Bearer ...`.
- `413` → file > `MAX_UPLOAD_BYTES`.
- `503` → backpressure: `MAX_CONCURRENT_PROVISIONS` reached. Something earlier is stuck — check Supabase for old rows in `provisioning`/`scanning`.
- CORS error → `ALLOWED_ORIGINS` doesn't include the dashboard origin.

## 2. Did the file land on disk?

On the API CT:

```bash
ls -la /var/brieblast/clients/<user_id>/
# expect <slug>-<site_id>.zip
```

Missing? The upload stream failed — check API logs (step 3).

## 3. API + worker logs (where 80% of failures show up)

```bash
journalctl -u briehost-api -f --output=cat
```

Re-upload from the dashboard, watch for:

- `running: ansible-playbook ...` → handing off to ansible
- `site_id=<uuid> status=scan_failed error=...` → scanner or zip-policy rejected it
- `provisioning crashed for site_id=...` → unhandled exception with stacktrace
- `could not persist final status` → Supabase write failed

Filter to one site:

```bash
journalctl -u briehost-api --since "10 min ago" | grep <site_id>
```

## 4. The Supabase row tells you what stage you're stuck in

```sql
select id, status, error_message, updated_at
from public.sites
where id = '<site_id>';
```

| Stuck on... | What's happening |
|---|---|
| `uploaded` | Worker never started — BackgroundTasks didn't fire, or API crashed right after insert |
| `scanning` | Zip-policy or clamd is hanging (a clamd that's reachable-but-broken would normally flip to `scan_failed`) |
| `scan_failed` | `error_message` tells you which: `zip policy: ...`, `malware: ...`, `scanner unavailable: ...`, `corrupt zip: ...` |
| `provisioning` | Ansible is running (or hung — capped at `ANSIBLE_TIMEOUT_SECONDS`, default 1800s) |
| `failed` | `error_message` has the tail of stderr/stdout from `ansible-playbook` |
| `live` | Done. |

## 5. Replay ansible by hand to see the real failure

When `error_message` is a stderr tail but not informative enough, run the exact command from the API CT with `-vvv`:

```bash
cd /opt/briehost-api
ansible-playbook -vvv \
  infra/ansible/playbooks/provision_site.yml \
  -i infra/ansible/inventory/production.ini \
  -e '{"site_id":"<site_id>","user_id":"<user_id>","zip_path":"/var/brieblast/clients/<user_id>/<slug>-<site_id>.zip","target_node":"pve","template_vmid":<vmid>}'
```

`-vvv` prints the underlying `pct` / SSH commands and their full stderr — usually where the actual problem becomes obvious.

## 6. On the Proxmox host (when ansible says it ran but the site is down)

```bash
ssh root@<proxmox-host>

pct list                                          # is the new CT there + running?
pct config <new_vmid> | grep description          # confirm tag: briehost site_id=...
pct exec <new_vmid> -- ls -la /var/www/html
pct exec <new_vmid> -- systemctl status nginx php*-fpm
pct exec <new_vmid> -- ip -4 -o addr show eth0    # got a DHCP lease?
pct exec <new_vmid> -- curl -sS -o /dev/null -w '%{http_code}\n' http://localhost/
```

If `curl` inside the CT returns 200 but the dashboard can't reach the site: networking / reverse-proxy problem (OPNsense forwarding, frontend proxy CT), not a provisioning problem.

## 7. "Is each subsystem alive" cheat sheet

Run on the API CT before blaming the pipeline:

```bash
# clamd reachable?
echo PING | nc -U /run/clamav/clamd.ctl       # expect "PONG"
# or for TCP:
echo PING | nc 127.0.0.1 3310

# ansible can reach Proxmox?
ansible -i /opt/briehost-api/infra/ansible/inventory/production.ini proxmox -m ping

# Supabase reachable from the CT?
curl -sI "$SUPABASE_URL/rest/v1/sites?select=id&limit=1" \
     -H "apikey: $SUPABASE_SERVICE_ROLE_KEY" \
     -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY"
```

## Common first-time-wiring failures

1. **Stuck at `scanning` → `scan_failed: scanner unavailable`** — clamd installed but listening on a unix socket the API user can't read, or TCP not enabled. `ls -la /run/clamav/clamd.ctl` and confirm the API process can read it.
2. **Stuck at `provisioning` → `failed: ansible rc=...`** with "permission denied" — Proxmox token ACL too narrow. Most often missing `PVEDatastoreUser` on `/storage/local-lvm`.
3. **Stuck at `provisioning` → `failed`** with "Could not resolve hostname" or SSH timeout — inventory `ansible_host` wrong, or the `from="<api-ct-ip>"` lock in the Proxmox host's `authorized_keys` doesn't match the API CT's actual IP.
4. **`live` but the dashboard shows nothing** — DHCP lease never landed; OPNsense pool exhausted or `tenant_bridge` wrong in `infra/ansible/inventory/group_vars/proxmox.yml`.
5. **`scan_failed: zip policy: Path traversal blocked`** on a legitimate zip — the customer zipped from the wrong directory and embedded an absolute path. Re-zip from inside the project root.
6. **`failed: ansible rc=4: [WARNING]: Collection community.general does not support Ansible version`** — Debian's `apt install ansible` is too old. Install ansible in the venv (`pip install "ansible>=10"`) and symlink the binaries into `/usr/local/bin` (see `PROXMOX_LXC_SETUP.md` § 4). Verify with `ansible --version` (need core ≥ 2.17).
7. **`failed: ansible rc=1: ERROR! the role 'X' was not found`** — `ansible.cfg` at the repo root not being picked up. The worker's `WorkingDirectory` must be the repo root (default `/opt/briehost-api`); confirm with `systemctl show -p WorkingDirectory briehost-api`.

## Per-task ansible visibility (recommended)

For deeper post-mortem on every playbook run (task-level timing, output, host facts) install **ARA** as a callback plugin on the API CT. See [`docs/ara.md`](docs/ara.md) for the wiring.
