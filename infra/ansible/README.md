# briehost ansible

Provisioning playbooks invoked by `app/worker.py` after a successful upload + scan.

Run manually:

```bash
ansible-playbook infra/ansible/playbooks/provision_site.yml \
  -i infra/ansible/inventory/production.ini \
  -e '{"site_id":"...","user_id":"...","zip_path":"/var/brieblast/clients/<uid>/<slug>-<site_id>.zip","target_node":"pve","template_vmid":9000}'
```

Roles:

- `roles/proxmox_cleanup` — destroys any tenant CT left behind by a prior failed run for the same `site_id` (matched on the `description: briehost site_id=<uuid>` tag).
- `roles/proxmox_clone` — picks a free VMID, full-clones the PHP template into the `briehost` pool, sets cores/memory/swap and a DHCP NIC on `tenant_bridge`, grows rootfs to `tenant_disk_gb`.
- `roles/start_container` — starts the CT, waits for `pct exec`, polls until OPNsense hands out a DHCP lease, records `tenant_ip`.
- `roles/deploy_site_zip` — `pct push`es the (already vetted) zip into the CT, runs the in-CT `/usr/local/bin/deploy-site.sh` (installed by the golden template) which wipes the docroot, unzips, and applies `www-data` ownership + `755`/`644` perms; then reloads php-fpm.
- `roles/healthcheck` — HTTP-GETs `tenant_ip` until it returns < 500.

Defaults live in `inventory/group_vars/proxmox.yml`. Public hostnames / TLS termination live outside this playbook (frontend reverse-proxy CT — see `PROXMOX_LXC_SETUP.md` § G).
