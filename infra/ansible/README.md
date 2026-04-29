# briehost ansible

Provisioning playbooks invoked by `app/worker.py` after a successful upload + scan.

Run manually:

```bash
ansible-playbook infra/ansible/playbooks/provision_site.yml \
  -i infra/ansible/inventory/production.ini \
  -e '{"site_id":"...","user_id":"...","zip_path":"/var/brieblast/clients/<uid>/<slug>-<site_id>.zip","target_node":"pve","template_vmid":9000}'
```

Roles are scaffolded as placeholders — fill in the real Proxmox calls in:

- `roles/proxmox_clone/tasks/main.yml`
- `roles/deploy_site_zip/tasks/main.yml`
- `roles/start_container/tasks/main.yml`
- `roles/healthcheck/tasks/main.yml`
