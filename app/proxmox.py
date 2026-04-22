"""Proxmox LXC provisioning. Currently a stub — the team fills in the real calls."""
from pathlib import Path

from app.config import Settings


def provision_php_site(settings: Settings, site_id: str, extracted_dir: Path) -> int:
    """
    Clone the PHP LXC template, push `extracted_dir` into /var/www/html, start the container.

    Returns the new container's VMID.

    TODO (team):
      - Use `proxmoxer.ProxmoxAPI(...)` with API-token auth (settings.proxmox_token_id / _secret).
      - `nodes/<node>/lxc/<template>/clone` to clone settings.php_template_vmid.
      - Wait for clone to finish (poll task status).
      - Push files: tarball + `pct push` or rsync over the bridged network.
      - `pct start <new_vmid>`.
    """
    raise NotImplementedError("Proxmox provisioning not yet wired up")
