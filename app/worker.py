"""Background provisioning worker.

Single-consumer asyncio queue. Provision and teardown jobs are FIFO-serialized
through one consumer so concurrent uploads can't race Proxmox (`pvesh get
/cluster/nextid` is informational and the LVM-thin template lock serializes
storage-side anyway). The consumer runs the sync pipeline in a thread executor
so it doesn't block the FastAPI event loop.

Compared to the previous threading.Lock + BackgroundTasks setup, jobs that are
waiting their turn now sit on status='queued' instead of pretending to be
provisioning — the UI can show queue position and the system stops looking
frozen to the second/third user.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import subprocess
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.config import Settings
from app.db import admin_client
from app.gateway import register_route, unregister_route
from app.health import record_health_check
from app.limits import get_user_plan_limits
from app.repo import RepoCloneError, clone_and_pack, validate_branch, validate_repo_url
from app.scanner import MalwareDetected, ScanError, clamd_scan
from app.storage import UnsafeZipError, site_zip_path, slugify, validate_zip_policy

log = logging.getLogger("briehost.worker")

# Status vocabulary, mirrored in ANSIBLE_PROVISIONING_APPROACH.md
STATUS_UPLOADED = "uploaded"
STATUS_QUEUED = "queued"
STATUS_SCANNING = "scanning"
STATUS_SCAN_FAILED = "scan_failed"
STATUS_PROVISIONING = "provisioning"
STATUS_UPDATING = "updating"
STATUS_LIVE = "live"
STATUS_FAILED = "failed"

SUPPORTED_BACKENDS = {"ansible"}

_TRIM = 4000  # cap for stderr/stdout we persist


# --- queue plumbing -------------------------------------------------------

JobKind = Literal["provision", "teardown", "redeploy"]


@dataclass(frozen=True)
class ProvisionJob:
    kind: Literal["provision"]
    site_id: str
    user_id: str
    zip_path: Path
    subdomain: str


@dataclass(frozen=True)
class TeardownJob:
    kind: Literal["teardown"]
    site_id: str
    subdomain: str | None
    zip_path: Path | None


@dataclass(frozen=True)
class RedeployJob:
    """Re-deploy a site to its existing CT from its stored repo URL/branch.

    No new clone/provision — `_redeploy_site_sync` re-pulls the repo, re-scans,
    then runs the redeploy ansible playbook against the site's existing vmid.
    """
    kind: Literal["redeploy"]
    site_id: str
    user_id: str
    repo_host: str
    repo_url: str
    repo_branch: str | None


Job = ProvisionJob | TeardownJob | RedeployJob

# Initialized in start_consumer() (needs to bind to the running event loop).
_queue: asyncio.Queue[Job | None] | None = None
_consumer_task: asyncio.Task | None = None
_running_site_id: str | None = None  # id of the job currently executing, None if idle


def queue_depth() -> int:
    """Pending + running jobs. Used as backpressure by the route layer."""
    pending = _queue.qsize() if _queue is not None else 0
    running = 1 if _running_site_id is not None else 0
    return pending + running


def queued_site_ids() -> list[str]:
    """Snapshot of site_ids currently waiting in the queue, in FIFO order.

    Used to compute queue position for the UI. asyncio.Queue exposes the
    internal deque as `._queue`; we read it without mutating.
    """
    if _queue is None:
        return []
    snapshot = list(_queue._queue)  # type: ignore[attr-defined]
    return [j.site_id for j in snapshot if isinstance(j, ProvisionJob)]


async def start_consumer(settings: Settings) -> None:
    """Call from FastAPI startup. Idempotent."""
    global _queue, _consumer_task
    if _consumer_task is not None and not _consumer_task.done():
        return
    _queue = asyncio.Queue()
    _consumer_task = asyncio.create_task(_consumer_loop(settings), name="provision-consumer")
    log.info("provision consumer started")


async def stop_consumer() -> None:
    """Call from FastAPI shutdown. Drains the consumer cleanly via sentinel."""
    global _queue, _consumer_task
    if _consumer_task is None:
        return
    if _queue is not None:
        await _queue.put(None)  # sentinel; consumer exits after this
    try:
        await asyncio.wait_for(_consumer_task, timeout=5)
    except asyncio.TimeoutError:
        log.warning("consumer did not exit in time; cancelling")
        _consumer_task.cancel()
    _consumer_task = None
    _queue = None


async def _consumer_loop(settings: Settings) -> None:
    """Single FIFO consumer. Runs each sync job in a thread executor so the
    long-running subprocess.run calls don't block other API requests."""
    global _running_site_id
    assert _queue is not None
    loop = asyncio.get_running_loop()
    while True:
        job = await _queue.get()
        try:
            if job is None:  # shutdown sentinel
                return
            _running_site_id = job.site_id
            try:
                if isinstance(job, ProvisionJob):
                    await loop.run_in_executor(
                        None,
                        _provision_site_sync,
                        settings,
                        job.site_id,
                        job.user_id,
                        job.zip_path,
                        job.subdomain,
                    )
                elif isinstance(job, RedeployJob):
                    await loop.run_in_executor(
                        None,
                        _redeploy_site_sync,
                        settings,
                        job.site_id,
                        job.user_id,
                        job.repo_host,
                        job.repo_url,
                        job.repo_branch,
                    )
                else:
                    await loop.run_in_executor(
                        None,
                        _teardown_site_sync,
                        settings,
                        job.site_id,
                        job.subdomain,
                        job.zip_path,
                    )
            except Exception:
                log.exception("consumer crashed on job site_id=%s", job.site_id)
            finally:
                _running_site_id = None
        finally:
            _queue.task_done()


# --- regex + status helpers ----------------------------------------------

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
    # Per-plan rootfs sizing. Read the user's plan and pick the matching
    # ceiling; `get_user_plan_limits` falls back to the most-restrictive tier
    # for unknown / NULL plan values, so a bad row defaults to the 8 GB
    # minimum rather than silently provisioning a huge disk.
    plan_limits = get_user_plan_limits(user_id)
    extra_vars: dict[str, object] = {
        "site_id": site_id,
        "user_id": user_id,
        "site_slug": site_slug,
        "zip_path": str(zip_path),
        "target_node": settings.proxmox_node,
        "template_vmid": settings.php_template_vmid,
        "tenant_disk_gb": plan_limits.lxc_disk_gb,
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


def _run_redeploy_ansible(
    settings: Settings, site_id: str, user_id: str, vmid: int, zip_path: Path
) -> tuple[int, str, str]:
    """Run the redeploy playbook against an *existing* CT. No clone, no IP
    allocation — just push the new zip and run deploy-site.sh."""
    site_slug = zip_path.stem.removesuffix(f"-{site_id}") or "site"
    extra_vars: dict[str, object] = {
        "site_id": site_id,
        "user_id": user_id,
        "site_slug": site_slug,
        "zip_path": str(zip_path),
        "new_vmid": vmid,
        "target_node": settings.proxmox_node,
    }
    try:
        extra_vars.update(json.loads(settings.ansible_extra_vars_json or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ANSIBLE_EXTRA_VARS_JSON is not valid JSON: {exc}") from exc

    cmd = [
        "ansible-playbook",
        settings.ansible_redeploy_playbook_path,
        "-i",
        settings.ansible_inventory_path,
        "-e",
        json.dumps(extra_vars),
    ]
    log.info("running redeploy: %s", " ".join(shlex.quote(c) for c in cmd))
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


# --- sync job bodies (executed off the event loop) ------------------------


def _provision_site_sync(
    settings: Settings,
    site_id: str,
    user_id: str,
    zip_path: Path,
    subdomain: str,
) -> None:
    """Full pipeline for one upload. Runs on a thread executor so the calling
    asyncio consumer stays responsive — but the consumer awaits this, so only
    one provision pipeline executes at a time."""
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

            try:
                record_health_check(site_id, "up", None, 200, None)
            except Exception:
                log.exception("could not persist initial health check for site_id=%s", site_id)

            # Gateway route push. The subdomain was already claimed at /provision
            # time, so we just need to point Caddy at the freshly-allocated IP.
            if subdomain and ip:
                try:
                    register_route(settings, subdomain, ip)
                except Exception:
                    log.exception(
                        "gateway route push failed for site_id=%s subdomain=%s",
                        site_id,
                        subdomain,
                    )
        else:
            tail = (stderr or stdout)[-_TRIM:]
            _set_status(site_id, STATUS_FAILED, f"ansible rc={rc}: {tail}")
    except Exception as exc:  # noqa: BLE001 — last-resort guard so worker never crashes silently
        log.exception("provisioning crashed for site_id=%s", site_id)
        _set_status_safe(site_id, STATUS_FAILED, f"worker crash: {exc}")


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


def _teardown_site_sync(
    settings: Settings,
    site_id: str,
    subdomain: str | None,
    zip_path: Path | None,
) -> None:
    """Best-effort tenant teardown: gateway route, LXC, on-disk zip.

    Called by the consumer after the API has already removed the DB row (so
    the dashboard reflects the delete instantly). Each step is logged but
    never raised — there's no row to mark "failed" on anymore, so failures
    here just become orphans for ops to clean up.
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


def _redeploy_site_sync(
    settings: Settings,
    site_id: str,
    user_id: str,
    repo_host: str,
    repo_url: str,
    repo_branch: str | None,
) -> None:
    """Refresh a live site's contents from its stored repo. CT, subdomain, and
    gateway route stay put — we just re-pull the repo, re-scan, and push the
    new zip into the existing CT via deploy-site.sh.

    On failure we mark the row 'failed' (with an error message). The CT keeps
    serving whatever was there before; user can hit redeploy again after fixing
    the upstream repo.
    """
    try:
        # Look up the CT's vmid — we can't redeploy without it.
        rows = (
            admin_client()
            .table("sites")
            .select("vmid, original_filename, name")
            .eq("id", site_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not rows:
            log.error("redeploy: site %s vanished from DB before worker ran", site_id)
            return
        site = rows[0]
        vmid = site.get("vmid")
        if not vmid:
            _set_status(site_id, STATUS_FAILED, "redeploy: site has no vmid (was it ever live?)")
            return

        _set_status(site_id, STATUS_UPDATING)

        try:
            host, owner, repo = validate_repo_url(repo_url)
            cleaned_branch = validate_branch(repo_branch)
        except RepoCloneError as exc:
            _set_status(site_id, STATUS_FAILED, f"redeploy: bad repo url: {exc}")
            return

        slug = slugify(repo)
        target = site_zip_path(settings.storage_root, user_id, site_id, display_name=slug)

        try:
            _meta, written = clone_and_pack(
                host=host,
                owner=owner,
                repo=repo,
                branch=cleaned_branch,
                target_zip=target,
                timeout_seconds=settings.repo_clone_timeout_seconds,
                max_files=settings.repo_clone_max_files,
                max_bytes=settings.repo_clone_max_bytes,
            )
        except RepoCloneError as exc:
            _set_status(site_id, STATUS_FAILED, f"redeploy clone failed: {exc}")
            return

        if written > settings.max_upload_bytes:
            target.unlink(missing_ok=True)
            _set_status(site_id, STATUS_FAILED, "redeploy: packaged repo exceeds upload size limit")
            return

        # Same zip + malware pipeline as a fresh provision — same security
        # posture for fresh uploads and updates.
        try:
            validate_zip_policy(
                target,
                max_files=settings.max_zip_files,
                max_uncompressed_bytes=settings.max_zip_uncompressed_bytes,
                max_compression_ratio=settings.max_zip_compression_ratio,
            )
        except (UnsafeZipError, zipfile.BadZipFile, OSError) as exc:
            _set_status(site_id, STATUS_FAILED, f"redeploy zip policy: {exc}")
            return

        if settings.enable_malware_scan:
            try:
                _scan_with_php_malware_finder(settings, target)
            except MalwareDetected as exc:
                _set_status(site_id, STATUS_FAILED, f"redeploy php malware: {exc}")
                return
            except ScanError as exc:
                _set_status(site_id, STATUS_FAILED, f"redeploy php scanner unavailable: {exc}")
                return

            try:
                clamd_scan(
                    target,
                    settings.clamd_host,
                    settings.clamd_port,
                    settings.clamd_socket,
                )
            except MalwareDetected as exc:
                _set_status(site_id, STATUS_FAILED, f"redeploy malware: {exc}")
                return
            except ScanError as exc:
                _set_status(site_id, STATUS_FAILED, f"redeploy scanner unavailable: {exc}")
                return

        try:
            rc, stdout, stderr = _run_redeploy_ansible(settings, site_id, user_id, int(vmid), target)
        except subprocess.TimeoutExpired as exc:
            _set_status(
                site_id,
                STATUS_FAILED,
                f"redeploy ansible timed out after {settings.ansible_timeout_seconds}s: {exc}",
            )
            return

        if rc == 0:
            # Stay 'live' — site never changed identity, just got fresher bits.
            _set_status(
                site_id,
                STATUS_LIVE,
                extra={"last_deploy_at": datetime.now(timezone.utc).isoformat()},
            )
        else:
            tail = (stderr or stdout)[-_TRIM:]
            _set_status(site_id, STATUS_FAILED, f"redeploy ansible rc={rc}: {tail}")
    except Exception as exc:  # noqa: BLE001
        log.exception("redeploy crashed for site_id=%s", site_id)
        _set_status_safe(site_id, STATUS_FAILED, f"redeploy worker crash: {exc}")


# --- public enqueue API (called from routes) ------------------------------


def enqueue_provision(
    settings: Settings,
    site_id: str,
    user_id: str,
    zip_path: Path,
    subdomain: str,
) -> None:
    """Add a provision job to the FIFO queue and mark the site as queued.

    The status flip happens here (not inside the consumer) so the dashboard
    immediately shows the row as 'queued' instead of sitting on 'uploaded'
    until the consumer picks it up.
    """
    if _queue is None:
        raise RuntimeError("provision consumer is not running; enqueue called before startup")
    _set_status_safe(site_id, STATUS_QUEUED)
    _queue.put_nowait(
        ProvisionJob(
            kind="provision",
            site_id=site_id,
            user_id=user_id,
            zip_path=zip_path,
            subdomain=subdomain,
        )
    )


def enqueue_redeploy(
    settings: Settings,
    site_id: str,
    user_id: str,
    repo_host: str,
    repo_url: str,
    repo_branch: str | None,
) -> None:
    """Add a redeploy job to the FIFO queue.

    Shares the queue with provision/teardown so `pct exec` calls into the
    tenant CT stay serialized and can't race a concurrent provision finishing
    on the same Proxmox host.
    """
    if _queue is None:
        raise RuntimeError("provision consumer is not running; enqueue called before startup")
    _set_status_safe(site_id, STATUS_QUEUED)
    _queue.put_nowait(
        RedeployJob(
            kind="redeploy",
            site_id=site_id,
            user_id=user_id,
            repo_host=repo_host,
            repo_url=repo_url,
            repo_branch=repo_branch,
        )
    )


def enqueue_teardown(
    settings: Settings,
    site_id: str,
    subdomain: str | None,
    zip_path: Path | None,
) -> None:
    """Add a teardown job to the same FIFO queue as provisions.

    Sharing the queue keeps the Proxmox `pct` interactions strictly serialized
    so a delete can't race a provision through the storage-layer lock.
    """
    if _queue is None:
        raise RuntimeError("provision consumer is not running; enqueue called before startup")
    _queue.put_nowait(
        TeardownJob(
            kind="teardown",
            site_id=site_id,
            subdomain=subdomain,
            zip_path=zip_path,
        )
    )
