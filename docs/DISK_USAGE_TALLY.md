# Per-CT Webapp Disk Usage Tally (proposal)

Status: **sketch / not implemented**. Filed as a follow-up to the per-plan
LXC sizing work (PR `yr/per-plan-disk-size`) so the quota math eventually
reflects what users are actually consuming inside their container instead
of the compressed zip they uploaded.

## Why

Today `sites.size_bytes` is set to the **compressed zip byte count** at
upload time and is the only number `app/limits.py::_sum_user_storage`
sums when enforcing plan caps. Two problems:

1. **Compressed sizes lie about real usage.** A 10 MB zip might expand
   to 200 MB of PHP + node_modules + assets inside the CT. A user could
   in principle stay well under their displayed quota while filling their
   rootfs.
2. **Post-upload growth is invisible.** Once provisioned, the tenant can
   push files via SFTP / git / phpMyAdmin / wp-admin uploads. None of
   that touches `sites.size_bytes`, so the API never notices growth.

We also can't enforce "delete some stuff to make room" cleanly today —
the only way to lower `_sum_user_storage` is to delete a whole site,
because the per-site number is frozen at upload time.

## Goal

Replace (or supplement) the zip-size tally with the real on-disk size of
each tenant's webapp directory, refreshed periodically and on demand.

## Approach

### Schema

```sql
ALTER TABLE sites
  ADD COLUMN disk_used_bytes BIGINT NULL,
  ADD COLUMN disk_used_at    TIMESTAMPTZ NULL;
```

Nullable on purpose: existing rows and freshly-uploaded-but-not-yet-live
rows have no measurement yet. `app/limits.py` falls back to
`COALESCE(disk_used_bytes, size_bytes)` so the new column is a strict
upgrade over current behavior.

### Measurement

Run from the briehost-api host (which already has Proxmox API creds and
the worker queue):

```
pct exec <vmid> -- du -sb /var/www/html
```

Pros: no extra agent inside the CT, works on stopped containers (with
`pct start`-on-demand if needed), `-sb` gives raw bytes.

Cons: only works on the node where the CT lives — multi-node would need
the `community.proxmox` collection or a direct API call to route to the
right node. Single-node today, so park that.

### Where the refresh runs

Three triggers, all funnel through the existing asyncio queue (`worker.py`)
so they're serialized with provision/teardown and can't race Proxmox
locks:

1. **On provision success.** Right after `STATUS_LIVE` is set, enqueue
   a refresh job for the new site. Initial value is populated within
   seconds of the CT coming up.
2. **Periodic sweep.** A new coroutine launched from `start_consumer`
   wakes every N hours, queries for sites with
   `disk_used_at < now() - interval 'N hours'`, and enqueues a refresh
   per stale row.
3. **On demand.** `POST /api/sites/{site_id}/refresh-usage` — rate-limited
   per user (e.g. 1/min) — enqueues a single refresh and returns the
   job-was-queued response. Dashboard wires this to a "↻" button beside
   the storage bar.

A new `RefreshUsageJob` dataclass next to `ProvisionJob` / `TeardownJob`
keeps the queue typed.

### Limits + dashboard wiring

- `app/limits.py::_sum_user_storage` → switch the select to
  `disk_used_bytes, size_bytes` and sum `disk_used_bytes ?? size_bytes`
  per row. No other limits.py changes needed.
- Frontend `DashboardPage.tsx` reads the new columns from Supabase
  (`useSites` hook already does `select *`). Render "Last updated 2 min
  ago" next to the storage bar; show the refresh button when
  `disk_used_at` is null or older than ~5 min.

## Considerations

- **du can be slow.** Big WordPress installs with millions of files take
  tens of seconds. Set a 60 s timeout per `pct exec` call; on timeout,
  log + keep the stale value instead of nulling it out.
- **What counts toward the quota.** Strawman: `/var/www/html` only. OS
  + `/var/log` + composer caches shouldn't burn the user's quota. Worth
  revisiting if a user complains they can't use their full allocation
  because we're measuring the wrong directory.
- **CT down / unreachable.** Treat as a soft failure: don't update
  `disk_used_at`, leave the stale value in place. Quota stays enforceable
  off the last known good measurement.
- **Future: managed databases.** When MySQL / Postgres land for tenants,
  their `.ibd` / pg cluster files live outside `/var/www/html`. Either
  measure those separately and sum, or move databases into a known path
  we already measure.
- **Migration of existing sites.** No backfill job needed — the periodic
  sweep picks up everyone with `disk_used_at IS NULL` on its first run.

## Open questions

- Periodic sweep interval: hourly? every 6 h? (depends on how much we
  trust the on-demand button)
- Should we cap the on-demand refresh per user, per CT, or both?
- Do we surface "your CT is using X but your quota counts Y" anywhere,
  or do we just silently switch the tally?

## Out of scope for this sketch

- Multi-node routing (single-node today)
- Quota enforcement on real-time growth inside the CT — that needs LXC
  cgroup limits, which is a separate hardening task
- Per-file storage breakdown for the user (treemap, "what's eating my
  quota") — nice-to-have but a UI project, not a backend one
