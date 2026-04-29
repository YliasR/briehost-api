"""Background provisioning worker.

Runs the scan -> ansible-playbook pipeline and writes status transitions to Supabase.
Invoked via FastAPI BackgroundTasks for now; can be moved to Celery/RQ/Arq later
without touching the route layer (see `enqueue_provision`).
"""
from __future__ import annotations

import json
import logging
import shlex
import subprocess
import zipfile
from pathlib import Path

from app.config import Settings
from app.db import admin_client
from app.scanner import MalwareDetected, ScanError, clamd_scan
from app.storage import UnsafeZipError, validate_zip_policy

log = logging.getLogger("briehost.worker")

# Status vocabulary, mirrored in ANSIBLE_PROVISIONING_APPROACH.md
STATUS_UPLOADED = "uploaded"
STATUS_SCANNING = "scanning"
STATUS_SCAN_FAILED = "scan_failed"
STATUS_PROVISIONING = "provisioning"
STATUS_LIVE = "live"
STATUS_FAILED = "failed"

_TRIM = 4000  # cap for stderr/stdout we persist


def _set_status(site_id: str, status: str, error: str | None = None) -> None:
    payload: dict[str, object] = {"status": status}
    if error is not None:
        payload["error_message"] = error[:_TRIM]
        log.warning("site_id=%s status=%s error=%s", site_id, status, error[:_TRIM])
    admin_client().table("sites").update(payload).eq("id", site_id).execute()


def _set_status_safe(site_id: str, status: str, error: str | None = None) -> None:
    """Best-effort status write for the last-resort handler — never raises."""
    try:
        _set_status(site_id, status, error)
    except Exception:
        log.exception("could not persist final status for site_id=%s", site_id)


def _run_ansible(settings: Settings, site_id: str, user_id: str, zip_path: Path) -> tuple[int, str, str]:
    extra_vars: dict[str, object] = {
        "site_id": site_id,
        "user_id": user_id,
        "zip_path": str(zip_path),
        "target_node": settings.proxmox_node,
        "template_vmid": settings.php_template_vmid,
    }
    try:
        extra_vars.update(json.loads(settings.ansible_extra_vars_json or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ANSIBLE_EXTRA_VARS_JSON is not valid JSON: {exc}") from exc

    cmd = [
        "ansible-playbook",
        settings.ansible_playbook_path,
        "-i",
        settings.ansible_inventory_path,
        "-e",
        json.dumps(extra_vars),
    ]
    log.info("running: %s", " ".join(shlex.quote(c) for c in cmd))
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def provision_site(settings: Settings, site_id: str, user_id: str, zip_path: Path) -> None:
    """Full pipeline for one upload. Safe to run as a fire-and-forget task."""
    try:
        _set_status(site_id, STATUS_SCANNING)

        try:
            validate_zip_policy(
                zip_path,
                max_files=settings.max_zip_files,
                max_uncompressed_bytes=settings.max_zip_uncompressed_bytes,
                max_compression_ratio=settings.max_zip_compression_ratio,
            )
        except UnsafeZipError as exc:
            _set_status(site_id, STATUS_SCAN_FAILED, f"zip policy: {exc}")
            return
        except zipfile.BadZipFile as exc:
            _set_status(site_id, STATUS_SCAN_FAILED, f"corrupt zip: {exc}")
            return
        except OSError as exc:
            _set_status(site_id, STATUS_SCAN_FAILED, f"zip read failed: {exc}")
            return

        if settings.enable_malware_scan:
            try:
                clamd_scan(
                    zip_path,
                    settings.clamd_host,
                    settings.clamd_port,
                    settings.clamd_socket,
                )
            except MalwareDetected as exc:
                _set_status(site_id, STATUS_SCAN_FAILED, f"malware: {exc}")
                return
            except ScanError as exc:
                # Scanner unreachable is treated as a hard fail — fail closed.
                _set_status(site_id, STATUS_SCAN_FAILED, f"scanner unavailable: {exc}")
                return

        _set_status(site_id, STATUS_PROVISIONING)

        rc, stdout, stderr = _run_ansible(settings, site_id, user_id, zip_path)
        if rc == 0:
            _set_status(site_id, STATUS_LIVE)
        else:
            tail = (stderr or stdout)[-_TRIM:]
            _set_status(site_id, STATUS_FAILED, f"ansible rc={rc}: {tail}")
    except Exception as exc:  # noqa: BLE001 — last-resort guard so worker never crashes silently
        log.exception("provisioning crashed for site_id=%s", site_id)
        _set_status_safe(site_id, STATUS_FAILED, f"worker crash: {exc}")


def enqueue_provision(
    background_tasks,  # fastapi.BackgroundTasks
    settings: Settings,
    site_id: str,
    user_id: str,
    zip_path: Path,
) -> None:
    """Single seam for swapping in Celery/RQ later. Today: FastAPI BackgroundTasks."""
    background_tasks.add_task(provision_site, settings, site_id, user_id, zip_path)
