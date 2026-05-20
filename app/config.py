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

    # Public-repo upload (git clone). Limits mirror the zip path so users can't
    # smuggle a larger payload by going through git instead of a file upload.
    repo_clone_timeout_seconds: int = int(os.getenv("REPO_CLONE_TIMEOUT_SECONDS", "60") or 60)
    repo_clone_max_bytes: int = int(
        os.getenv("REPO_CLONE_MAX_BYTES", str(100 * 1024 * 1024))
    )
    repo_clone_max_files: int = int(os.getenv("REPO_CLONE_MAX_FILES", "5000") or 5000)

    # Provisioning backend
    provisioner_backend: str = os.getenv("PROVISIONER_BACKEND", "ansible")
    ansible_playbook_path: str = os.getenv(
        "ANSIBLE_PLAYBOOK_PATH", "infra/ansible/playbooks/provision_site.yml"
    )
    ansible_delete_playbook_path: str = os.getenv(
        "ANSIBLE_DELETE_PLAYBOOK_PATH", "infra/ansible/playbooks/delete_site.yml"
    )
    ansible_inventory_path: str = os.getenv(
        "ANSIBLE_INVENTORY_PATH", "infra/ansible/inventory/production.ini"
    )
    ansible_extra_vars_json: str = os.getenv("ANSIBLE_EXTRA_VARS_JSON", "{}")
    # Hard wall-clock cap on a single ansible-playbook run so the worker thread
    # can't block forever on a stuck SSH/lock/etc. Default: 30 min.
    ansible_timeout_seconds: int = int(os.getenv("ANSIBLE_TIMEOUT_SECONDS", "1800") or 1800)
    # Backpressure: max concurrent provisioning jobs. New uploads beyond this
    # are rejected with 503 until in-flight jobs drain. Real fix is a job queue.
    max_concurrent_provisions: int = int(os.getenv("MAX_CONCURRENT_PROVISIONS", "3") or 3)

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

    # Public subdomain gateway (Caddy + cloudflared on a separate CT).
    # Empty url disables registration — useful for local dev.
    # See CLOUDFLARE_GATEWAY_SETUP.md.
    gateway_caddy_admin_url: str = os.getenv("GATEWAY_CADDY_ADMIN_URL", "")
    gateway_domain: str = os.getenv("GATEWAY_DOMAIN", "")
    gateway_request_timeout_seconds: int = int(
        os.getenv("GATEWAY_REQUEST_TIMEOUT_SECONDS", "5") or 5
    )

    # Runtime health checks for live sites. The public /status page only counts
    # sites with a recent passing check as online.
    healthcheck_enabled: bool = _bool("HEALTHCHECK_ENABLED", "true")
    healthcheck_interval_seconds: int = int(os.getenv("HEALTHCHECK_INTERVAL_SECONDS", "60") or 60)
    healthcheck_timeout_seconds: float = float(os.getenv("HEALTHCHECK_TIMEOUT_SECONDS", "5") or 5)
    healthcheck_max_concurrency: int = int(os.getenv("HEALTHCHECK_MAX_CONCURRENCY", "10") or 10)
    healthcheck_public_scheme: str = os.getenv("HEALTHCHECK_PUBLIC_SCHEME", "https").strip() or "https"

    allowed_origins: list[str] = [
        o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",") if o.strip()
    ]
    api_host: str = os.getenv("API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("API_PORT", "8000") or 8000)

    # --- Payments (all sandbox / test mode). See docs/PAYMENTS.md.
    # Each provider is independently optional; missing keys → 503 from that
    # provider's endpoint, other providers still work.
    payments_return_base_url: str = os.getenv("PAYMENTS_RETURN_BASE_URL", "http://localhost:5173")

    stripe_secret_key: str = os.getenv("STRIPE_SECRET_KEY", "")
    stripe_webhook_secret: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    paypal_env: str = os.getenv("PAYPAL_ENV", "sandbox")
    paypal_client_id: str = os.getenv("PAYPAL_CLIENT_ID", "")
    paypal_client_secret: str = os.getenv("PAYPAL_CLIENT_SECRET", "")
    paypal_webhook_id: str = os.getenv("PAYPAL_WEBHOOK_ID", "")

    coingate_api_key: str = os.getenv("COINGATE_API_KEY", "")
    coingate_api_base: str = os.getenv("COINGATE_API_BASE", "https://api-sandbox.coingate.com/v2")
    coingate_webhook_secret: str = os.getenv("COINGATE_WEBHOOK_SECRET", "")


@lru_cache
def get_settings() -> Settings:
    return Settings()
