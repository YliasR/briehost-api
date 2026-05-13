"""Background provisioning worker.

Runs the scan -> ansible-playbook pipeline and writes status transitions to Supabase.
Invoked via FastAPI BackgroundTasks for now; can be moved to Celery/RQ/Arq later
without touching the route layer (see `enqueue_provision`).
"""
from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

from app.config import Settings
from app.db import admin_client
from app.gateway import derive_subdomain, register_route, unregister_route
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

SUPPORTED_BACKENDS = {"ansible"}

_TRIM = 4000  # cap for stderr/stdout we persist

# In-process gauge of provisions currently running. Used by the route as
# crude backpressure (reject new uploads at capacity). Not durable across
# restarts — proper backpressure belongs in a real job queue.
_inflight_lock = threading.Lock()
_inflight = 0

# Serializes the ansible-playbook call across concurrent uploads. Proxmox
# clone+config operations race on two fronts:
#   1. `pvesh get /cluster/nextid` is informational, so two concurrent runs
#      can get the same VMID and the second clone fails (403 or "exists").
#   2. Full clones from the same LVM-thin template hit a storage-level lock
#      and one of the parallel runs gets "got lock timeout" (HTTP 500).
# Holding this lock means scan/validate still run in parallel; only the
# Proxmox interaction serializes. This is the bottleneck anyway.
_provision_lock = threading.Lock()


def inflight_count() -> int:
    with _inflight_lock:
        return _inflight


def _inflight_inc() -> None:
    global _inflight
    with _inflight_lock:
        _inflight += 1


def _inflight_dec() -> None:
    global _inflight
    with _inflight_lock:
        _inflight = max(0, _inflight - 1)


# Healthcheck role emits: "BRIEHOST_RESULT site_id=... vmid=... ip=... hostname=... status=live"
_RESULT_RE = re.compile(r"BRIEHOST_RESULT\s+(?P<kv>[^\"]+?)(?:\\n|\"|$)")
_KV_RE = re.compile(r"(\w+)=([^\s\"]+)")


def _parse_ansible_result(stdout: str) -> dict[str, str]:
    match = _RESULT_RE.search(stdout)
    if not match:
        return {}
    return dict(_KV_RE.findall(match.group("kv")))


def _set_status(
    site_id: str,
    status: str,
    error: str | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    payload: dict[str, object] = {"status": status}
    if error is not None:
        payload["error_message"] = error[:_TRIM]
        log.warning("site_id=%s status=%s error=%s", site_id, status, error[:_TRIM])
    if extra:
        payload.update(extra)
    admin_client().table("sites").update(payload).eq("id", site_id).execute()


def _set_status_safe(site_id: str, status: str, error: str | None = None) -> None:
    """Best-effort status write for the last-resort handler — never raises."""
    try:
        _set_status(site_id, status, error)
    except Exception:
        log.exception("could not persist final status for site_id=%s", site_id)


def _run_ansible(
    settings: Settings, site_id: str, user_id: str, zip_path: Path
) -> tuple[int, str, str]:
    # Filename is `<slug>-<site_id>.zip`; recover the slug for human-readable hostnames.
    site_slug = zip_path.stem.removesuffix(f"-{site_id}") or "site"
    extra_vars: dict[str, object] = {
        "site_id": site_id,
        "user_id": user_id,
        "site_slug": site_slug,
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
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=settings.ansible_timeout_seconds,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _zip_info_is_symlink(info: zipfile.ZipInfo) -> bool:
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def _scan_with_php_malware_finder(settings: Settings, zip_path: Path) -> None:
    scanner_path = getattr(settings, "php_malware_finder_path", None)
    if not scanner_path:
        scanner_path = shutil.which("phpmalwarefinder") or shutil.which("yara")
    if not scanner_path:
        raise ScanError("php-malware-finder scanner is not installed or configured")

    rules_path = getattr(settings, "php_malware_finder_rules_path", None)
    if not rules_path:
        scanner_candidate = Path(scanner_path)
        if scanner_candidate.exists():
            sibling_rules = scanner_candidate.resolve().with_name("php.yar")
            if sibling_rules.is_file():
                rules_path = str(sibling_rules)

    if not rules_path:
        bundled_rules = Path(__file__).resolve().with_name("php.yar")
        if bundled_rules.is_file():
            rules_path = str(bundled_rules)

    if not rules_path:
        raise ScanError("php-malware-finder rules are not installed or configured")

    timeout = getattr(
        settings,
        "php_malware_finder_timeout_seconds",
        getattr(settings, "ansible_timeout_seconds", 300),
    )

    with tempfile.TemporaryDirectory(prefix="php-scan-") as tmpdir:
        extract_dir = Path(tmpdir).resolve()

        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if _zip_info_is_symlink(info):
                    raise MalwareDetected(f"zip contains symlink entry: {info.filename}")

                target_path = (extract_dir / info.filename).resolve()
                if target_path != extract_dir and extract_dir not in target_path.parents:
                    raise MalwareDetected(f"zip contains unsafe path entry: {info.filename}")

                target_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target_path.open("wb") as destination:
                    shutil.copyfileobj(source, destination)

        command = [
            scanner_path,
            "-r" if Path(scanner_path).name == "yara" else "-c",
            rules_path,
            str(extract_dir),
        ]
        log.info("running php malware scan: %s", " ".join(shlex.quote(c) for c in command))
        proc = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if stdout:
            raise MalwareDetected(stdout[:_TRIM])
        if proc.returncode != 0 or stderr:
            raise ScanError(stderr[:_TRIM] or f"php-malware-finder exited with rc={proc.returncode}")


def provision_site(settings: Settings, site_id: str, user_id: str, zip_path: Path) -> None:
    """Full pipeline for one upload. Safe to run as a fire-and-forget task."""
    _inflight_inc()
    try:
        if settings.provisioner_backend not in SUPPORTED_BACKENDS:
            _set_status(
                site_id,
                STATUS_FAILED,
                f"unsupported provisioner backend: {settings.provisioner_backend!r}",
            )
            return

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
                _scan_with_php_malware_finder(settings, zip_path)
            except MalwareDetected as exc:
                _set_status(site_id, STATUS_SCAN_FAILED, f"php malware: {exc}")
                return
            except ScanError as exc:
                _set_status(site_id, STATUS_SCAN_FAILED, f"php scanner unavailable: {exc}")
                return

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

        try:
            with _provision_lock:
                rc, stdout, stderr = _run_ansible(settings, site_id, user_id, zip_path)
        except subprocess.TimeoutExpired as exc:
            _set_status(
                site_id,
                STATUS_FAILED,
                f"ansible timed out after {settings.ansible_timeout_seconds}s: {exc}",
            )
            return

        if rc == 0:
            # Mark live first so a failure in any of the post-live writes (extras,
            # subdomain, gateway push) can't flip a healthy site to "failed".
            _set_status(site_id, STATUS_LIVE)
            parsed = _parse_ansible_result(stdout)

            ip = parsed.get("ip")
            extra: dict[str, object] = {}
            if ip:
                extra["ip_address"] = ip
            if vmid := parsed.get("vmid"):
                try:
                    extra["vmid"] = int(vmid)
                except ValueError:
                    pass
            if extra:
                try:
                    admin_client().table("sites").update(extra).eq("id", site_id).execute()
                except Exception:
                    log.exception("could not persist live extras for site_id=%s", site_id)

            # Public subdomain assignment + gateway route push. Each is best-effort
            # and persisted in its own UPDATE so a unique-constraint collision on
            # subdomain doesn't lose the IP/VMID write above.
            site_slug = zip_path.stem.removesuffix(f"-{site_id}") or "site"
            subdomain = derive_subdomain(site_slug)
            if subdomain and ip:
                try:
                    register_route(settings, subdomain, ip)
                except Exception:
                    log.exception(
                        "gateway route push failed for site_id=%s subdomain=%s",
                        site_id,
                        subdomain,
                    )
                try:
                    admin_client().table("sites").update(
                        {"subdomain": subdomain}
                    ).eq("id", site_id).execute()
                except Exception:
                    # Most likely cause: unique-index collision with another tenant
                    # already holding this slug. The site is still live; the user
                    # can pick a different subdomain via the dashboard later.
                    log.exception(
                        "subdomain assign failed (collision?) for site_id=%s subdomain=%s",
                        site_id,
                        subdomain,
                    )
        else:
            tail = (stderr or stdout)[-_TRIM:]
            _set_status(site_id, STATUS_FAILED, f"ansible rc={rc}: {tail}")
    except Exception as exc:  # noqa: BLE001 — last-resort guard so worker never crashes silently
        log.exception("provisioning crashed for site_id=%s", site_id)
        _set_status_safe(site_id, STATUS_FAILED, f"worker crash: {exc}")
    finally:
        _inflight_dec()


def enqueue_provision(
    background_tasks,  # fastapi.BackgroundTasks
    settings: Settings,
    site_id: str,
    user_id: str,
    zip_path: Path,
) -> None:
    """Single seam for swapping in Celery/RQ later. Today: FastAPI BackgroundTasks."""
    background_tasks.add_task(provision_site, settings, site_id, user_id, zip_path)


def _run_delete_playbook(settings: Settings, site_id: str) -> tuple[int, str, str]:
    """Run the delete playbook (proxmox_cleanup) for a single site_id."""
    extra_vars: dict[str, object] = {"site_id": site_id}
    try:
        extra_vars.update(json.loads(settings.ansible_extra_vars_json or "{}"))
    except json.JSONDecodeError:
        # already validated on the provisioning path; ignore here so a malformed
        # extra-vars JSON doesn't block deletes.
        pass

    cmd = [
        "ansible-playbook",
        settings.ansible_delete_playbook_path,
        "-i",
        settings.ansible_inventory_path,
        "-e",
        json.dumps(extra_vars),
    ]
    log.info("running: %s", " ".join(shlex.quote(c) for c in cmd))
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=settings.ansible_timeout_seconds,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def teardown_site(
    settings: Settings,
    site_id: str,
    subdomain: str | None,
    zip_path: Path | None,
) -> None:
    """Best-effort tenant teardown: gateway route, LXC, on-disk zip.

    Designed to be called from a background task after the API has already
    removed the DB row (so the dashboard reflects the delete instantly). Each
    step is logged but never raised — there's no row to mark "failed" on
    anymore, so failures here just become orphans for ops to clean up.
    """
    if subdomain:
        try:
            unregister_route(settings, subdomain)
        except Exception:
            log.exception("gateway unregister failed for site_id=%s subdomain=%s", site_id, subdomain)

    if zip_path:
        try:
            Path(zip_path).unlink(missing_ok=True)
        except Exception:
            log.exception("zip cleanup failed for site_id=%s path=%s", site_id, zip_path)

    try:
        # Serialize against concurrent provisioning to avoid pct races.
        with _provision_lock:
            rc, stdout, stderr = _run_delete_playbook(settings, site_id)
        if rc != 0:
            log.error(
                "delete playbook failed for site_id=%s rc=%s tail=%s",
                site_id,
                rc,
                (stderr or stdout)[-_TRIM:],
            )
    except subprocess.TimeoutExpired:
        log.exception("delete playbook timed out for site_id=%s", site_id)
    except Exception:
        log.exception("delete playbook crashed for site_id=%s", site_id)


def enqueue_teardown(
    background_tasks,
    settings: Settings,
    site_id: str,
    subdomain: str | None,
    zip_path: Path | None,
) -> None:
    background_tasks.add_task(teardown_site, settings, site_id, subdomain, zip_path)
