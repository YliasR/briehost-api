"""Deprecated: provisioning now runs via Ansible (see app/worker.py and infra/ansible/).

Kept as a thin marker so older imports fail loudly instead of silently doing nothing.
"""
from pathlib import Path
from typing import NoReturn

from app.config import Settings


def provision_php_site(settings: Settings, site_id: str, extracted_dir: Path) -> NoReturn:
    raise RuntimeError(
        "provision_php_site() is deprecated; provisioning is handled by app.worker via Ansible"
    )
