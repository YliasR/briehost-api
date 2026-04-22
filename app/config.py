import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


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

    allowed_origins: list[str] = [
        o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",") if o.strip()
    ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
