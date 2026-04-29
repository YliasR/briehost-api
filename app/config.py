import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    supabase_url: str = os.environ["SUPABASE_URL"]
    supabase_service_role_key: str = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    supabase_jwt_audience: str = os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated")

    proxmox_host: str = os.getenv("PROXMOX_HOST", "")
    proxmox_user: str = os.getenv("PROXMOX_USER", "")
    proxmox_token_id: str = os.getenv("PROXMOX_TOKEN_ID", "")
    proxmox_token_secret: str = os.getenv("PROXMOX_TOKEN_SECRET", "")
    proxmox_node: str = os.getenv("PROXMOX_NODE", "pve")
    php_template_vmid: int = int(os.getenv("PHP_TEMPLATE_VMID", "0") or 0)

    storage_root: str = os.getenv("STORAGE_ROOT", "/var/brieblast/clients")
    max_upload_bytes: int = int(os.getenv("MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))

    # Provisioning backend
    provisioner_backend: str = os.getenv("PROVISIONER_BACKEND", "ansible")
    ansible_playbook_path: str = os.getenv(
        "ANSIBLE_PLAYBOOK_PATH", "infra/ansible/playbooks/provision_site.yml"
    )
    ansible_inventory_path: str = os.getenv(
        "ANSIBLE_INVENTORY_PATH", "infra/ansible/inventory/production.ini"
    )
    ansible_extra_vars_json: str = os.getenv("ANSIBLE_EXTRA_VARS_JSON", "{}")

    # Malware / zip policy
    enable_malware_scan: bool = _bool("ENABLE_MALWARE_SCAN", "true")
    clamd_socket: str = os.getenv("CLAMD_SOCKET", "/run/clamav/clamd.ctl")
    clamd_host: str = os.getenv("CLAMD_HOST", "127.0.0.1")
    clamd_port: int = int(os.getenv("CLAMD_PORT", "3310") or 3310)
    max_zip_files: int = int(os.getenv("MAX_ZIP_FILES", "5000") or 5000)
    max_zip_uncompressed_bytes: int = int(
        os.getenv("MAX_ZIP_UNCOMPRESSED_BYTES", str(500 * 1024 * 1024))
    )
    max_zip_compression_ratio: int = int(os.getenv("MAX_ZIP_COMPRESSION_RATIO", "200") or 200)

    allowed_origins: list[str] = [
        o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",") if o.strip()
    ]
    api_host: str = os.getenv("API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("API_PORT", "8000") or 8000)


@lru_cache
def get_settings() -> Settings:
    return Settings()
