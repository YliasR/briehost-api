# Storage Quotas & Proxmox Container Sizing

## 1. The Discrepancy
There was a reported issue where a very small ZIP upload (~10 MB) appeared to instantly consume approx 8 GB of storage, maxing out the `smol_brie` plan limits. 

## 2. API Quota Enforcement (`app/limits.py`)
The API strictly enforces limits based on the **compressed ZIP file size** at upload time (`size_bytes`), not the volume of the resulting Proxmox container. 
- During `POST /upload`, the streaming chunks are tallied into the `written` variable, and this exact byte count is inserted into the `sites` table under `size_bytes`.
- The storage sum (`_sum_user_storage`) counts the `size_bytes` for **all** of the user's sites, including `failed`, `scan_failed`, or `queued` sites. 
- The API currently has zero awareness of the actual Proxmox LXC disk size.

## 3. Proxmox Provisioning (`infra/ansible/...`)
By default, **every** provisioned site receives exactly an **8 GB thin-provisioned disk**, regardless of the user's plan.
- Defined in: `infra/ansible/inventory/group_vars/proxmox.yml` (`tenant_disk_gb: 8`).
- Since it is thin-provisioned (via `local-lvm`), a fresh PHP site essentially only consumes the space of the base OS and the uncompressed ZIP contents on the host node's physical SSD.

## 4. Why did a 10MB file trigger an 8GB quota alert?
Because the backend strictly tallies the compressed zip sizes, the "8 GB" quota usage hit is NOT coming from a single 10MB zip upload magically expanding to 8GB inside the PostgreSQL database. It is highly likely caused by one of two scenarios:
1. **Accumulated Failed Uploads:** The user had previous massive, failed, or stuck uploads. Because the API counts *all* site status rows towards the quota to prevent bypass abuse, dangling failures can lock an account.
2. **Dashboard / Proxmox UI Confusion:** Given that `tenant_disk_gb` is exactly 8, the user may have seen the 8GB rootfs allocation in the Proxmox UI (or an exposed metric) rather than the actual `size_bytes` quota from the backend. 

## 5. Plans & Future Improvements
Currently, plan scaling limits the *aggregate zip upload size* and *total number of sites*.
- `smol_brie`: 1 Site, 10 GB limit (recently bumped from 5 GB).
- `thicc_brie`: Unlimited Sites, 50 GB limit.
- `mega_brie`: Unlimited Sites, 200 GB limit.

### Proposed Architecture Update
If we want the higher-tier plans to grant larger *individual* container disks instead of just allowing more 8GB containers, the backend (`app/worker.py`) should fetch the user's plan and pass a dynamic `tenant_disk_gb` variable down to the Ansible playbook based on their specific plan, overriding the default 8GB.