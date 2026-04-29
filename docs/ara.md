# ARA — web UI for ansible runs

ARA ("Ansible Records Ansible") attaches as a **callback plugin** to `ansible-playbook`, captures every play/task/host result into a database, and serves a web UI. No invocation changes — `app/worker.py` keeps calling `ansible-playbook` exactly the same way; ARA just observes.

This is the cheapest way to get "what failed, on which task, with what output" across many runs without standing up AWX.

## Install (on the API CT, same venv as the API)

```bash
. /opt/briehost-api/.venv/bin/activate
pip install "ara[server]"
```

Discover the callback plugin path:

```bash
python -c 'import ara.setup; print(ara.setup.callback_plugins)'
# e.g. /opt/briehost-api/.venv/lib/python3.11/site-packages/ara/plugins/callback
```

## Enable the callback for the worker's runs

Add to `/opt/briehost-api/.env` (loaded by the systemd unit, so it ends up in the worker's environment when it shells out to `ansible-playbook`):

```env
ANSIBLE_CALLBACK_PLUGINS=/opt/briehost-api/.venv/lib/python3.11/site-packages/ara/plugins/callback
ANSIBLE_CALLBACKS_ENABLED=ara_default
ARA_API_CLIENT=offline
ARA_DATABASE_NAME=/opt/briehost-api/ara.sqlite
```

`ARA_API_CLIENT=offline` writes directly to the SQLite DB — no separate API daemon. Fine for our scale; switch to `http` + a proper Postgres if you outgrow it.

`systemctl restart briehost-api` and trigger one upload to confirm a row lands in `ara.sqlite`.

## Two CLIs, don't confuse them

ARA ships **two** entry points:

- **`ara-manage`** — Django management. Use it for DB setup and the web server: `ara-manage migrate`, `ara-manage runserver`, `ara-manage settings`.
- **`ara`** — records-query CLI. Use it to inspect captured runs: `ara playbook list`, `ara task show <id>`, `ara result show <id>`.

Both honor the same `ARA_*` env vars, so `set -a; . /opt/briehost-api/.env; set +a` before invoking them from a shell.

Common queries:

```bash
ara playbook list                              # recent runs
ara playbook list --status failed --limit 10
ara playbook show <id>                         # one run with its plays
ara task list --playbook <id> --order=-duration
ara task list --status failed
ara task show <id>
ara result show <id>                           # actual stdout/stderr
ara playbook prune --days 30                   # cleanup
```

Add `-f json` / `-f yaml` to pipe into `jq` etc.

## Run the web UI

```bash
ara-manage runserver 0.0.0.0:9191
```

For production, run it as its own systemd unit:

```ini
# /etc/systemd/system/ara-web.service
[Unit]
Description=ARA web UI
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/briehost-api
EnvironmentFile=/opt/briehost-api/.env
ExecStart=/opt/briehost-api/.venv/bin/ara-manage runserver 0.0.0.0:9191
Restart=always
User=root

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now ara-web
```

Browse to `http://<api-ct-ip>:9191`. Each provision = one playbook run; click in to see per-task stdout/stderr, durations, and which host the task ran against. Failures are colored and filterable.

## What this gives you for troubleshooting

- Every Ansible run from `app/worker.py` shows up automatically.
- For a `failed` site in Supabase: copy the `site_id`, search the ARA UI for it (it's in the `site_id` extra-var), see exactly which task failed and the full stderr — no need to replay the playbook by hand.
- Side-by-side comparison of two runs to see what changed (config drift, template version, etc).

## When you'd want something heavier instead

- **AWX** (Red Hat's upstream of Ansible Tower) — full job-scheduling UI, RBAC, surveys, inventory sync. Runs on Kubernetes via the AWX Operator. Overkill here, since `app/worker.py` already triggers playbooks; AWX wants to *be* the trigger.
- **Semaphore** — lighter than AWX (single Go binary, Postgres backend), good if you eventually want a UI to launch ad-hoc runs. Still replaces the trigger model — you'd POST to the Semaphore API from the worker instead of shelling out to `ansible-playbook`.

ARA is recommended for now precisely because it's purely additive: zero changes to how the worker runs ansible.
