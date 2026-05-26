"""Site upload + teardown endpoints."""
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile, status

from app.auth import current_user_id
from app.config import Settings, get_settings
from app.db import admin_client
from app.gateway import derive_subdomain
from app.limits import (
    assert_can_create_site,
    assert_within_site_count,
    assert_within_storage,
    get_user_plan,
    get_user_plan_limits,
)
from app.repo import RepoCloneError, clone_and_pack, validate_branch, validate_repo_url
from app.storage import site_zip_path, slugify
from app.worker import (
    STATUS_LIVE,
    STATUS_QUEUED,
    STATUS_UPLOADED,
    enqueue_provision,
    enqueue_redeploy,
    enqueue_teardown,
    enqueue_zip_redeploy,
    queue_depth,
)

log = logging.getLogger("briehost.routes.sites")

router = APIRouter(prefix="/api/sites", tags=["sites"])


@router.post("/upload")
async def upload_site(
    file: UploadFile = File(...),
    user_id: str = Depends(current_user_id),
    settings: Settings = Depends(get_settings),
):
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "File must be a .zip")

    # Cheap upfront site-count check — bail out before streaming a multi-MB
    # body if the user is already at their plan's site cap. Storage check
    # happens below, after we know the actual upload size.
    plan_limits = get_user_plan_limits(user_id)
    assert_within_site_count(user_id, plan_limits)

    site_id = str(uuid.uuid4())
    name = Path(file.filename).stem or "site"
    slug = slugify(name)
    target = site_zip_path(settings.storage_root, user_id, site_id, display_name=slug)

    written = 0
    try:
        with target.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > settings.max_upload_bytes:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File exceeds 100 MB")
                out.write(chunk)
    except HTTPException:
        target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to store uploaded file",
        ) from exc

    # Storage check now that we know the real size. If it fails, drop the
    # file we just wrote — no DB row was created yet so cleanup is just the
    # filesystem.
    try:
        assert_within_storage(user_id, plan_limits, written)
    except HTTPException:
        target.unlink(missing_ok=True)
        raise

    # Suggest a default subdomain from the filename so the UI can prefill it,
    # but don't claim it yet — that happens at /provision time after the user
    # confirms (and possibly edits) the name.
    suggested_subdomain = derive_subdomain(slug)

    admin_client().table("sites").insert(
        {
            "id": site_id,
            "user_id": user_id,
            "name": name,
            "original_filename": file.filename,
            "size_bytes": written,
            "status": STATUS_UPLOADED,
        }
    ).execute()

    return {
        "siteId": site_id,
        "status": STATUS_UPLOADED,
        "suggestedSubdomain": suggested_subdomain,
    }


@router.post("/upload-repo")
async def upload_site_from_repo(
    payload: dict = Body(...),
    user_id: str = Depends(current_user_id),
    settings: Settings = Depends(get_settings),
):
    """Mirror /upload but pull the site contents from a public GitHub URL.

    Returns the same shape so the frontend can hand off to /provision the
    same way it does for a zip upload.
    """
    repo_url = (payload.get("repoUrl") or payload.get("repo_url") or "").strip()
    branch = (payload.get("branch") or "").strip() or None
    if not repo_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "repoUrl is required")

    # Upfront count check before we do anything expensive (git clone, zip).
    # Storage check has to happen after clone_and_pack since we can't know
    # the packed size in advance.
    plan_limits = get_user_plan_limits(user_id)
    assert_within_site_count(user_id, plan_limits)

    site_id = str(uuid.uuid4())
    # Resolve owner/repo up front so the on-disk filename matches what the
    # /provision endpoint will reconstruct from `original_filename` later.
    # `validate_repo_url` is cheap and raises the same RepoCloneError that
    # clone_and_pack would, so error handling stays uniform.
    try:
        host, owner, repo = validate_repo_url(repo_url)
        cleaned_branch = validate_branch(branch)
    except RepoCloneError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    slug = slugify(repo)
    original_filename = f"{slug}.zip"
    target = site_zip_path(settings.storage_root, user_id, site_id, display_name=slug)

    try:
        meta, written = clone_and_pack(
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
        target.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except Exception as exc:
        log.exception("repo clone failed unexpectedly")
        target.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to import repository",
        ) from exc

    if written > settings.max_upload_bytes:
        target.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "Packaged repo exceeds upload size limit",
        )

    # Storage check — same pattern as /upload. Clean up the packed zip if
    # we're over the plan cap.
    try:
        assert_within_storage(user_id, plan_limits, written)
    except HTTPException:
        target.unlink(missing_ok=True)
        raise

    name = f"{meta.owner}/{meta.repo}"
    if meta.ref != "default":
        name = f"{name}@{meta.ref}"
    suggested_subdomain = derive_subdomain(meta.repo)

    admin_client().table("sites").insert(
        {
            "id": site_id,
            "user_id": user_id,
            "name": name,
            "original_filename": original_filename,
            "size_bytes": written,
            "status": STATUS_UPLOADED,
            # Persist the source so a later one-click redeploy can re-pull
            # without making the user re-enter the URL + branch.
            "repo_host": host,
            "repo_url": repo_url,
            "repo_branch": cleaned_branch,
        }
    ).execute()

    return {
        "siteId": site_id,
        "status": STATUS_UPLOADED,
        "suggestedSubdomain": suggested_subdomain,
    }


@router.post("/{site_id}/provision")
async def provision_uploaded_site(
    site_id: str,
    payload: dict = Body(...),
    user_id: str = Depends(current_user_id),
    settings: Settings = Depends(get_settings),
):
    """Kick off provisioning for a previously-uploaded site under the chosen subdomain."""
    raw_subdomain = (payload.get("subdomain") or "").strip()
    if not raw_subdomain:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "subdomain is required")

    subdomain = derive_subdomain(raw_subdomain)
    if not subdomain:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "subdomain must contain at least one letter or digit",
        )

    rows = (
        admin_client()
        .table("sites")
        .select("id, user_id, status, original_filename, name")
        .eq("id", site_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "site not found")
    site = rows[0]

    if site.get("status") != STATUS_UPLOADED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"site is not awaiting provisioning (status={site.get('status')})",
        )

    # Backpressure: queue depth includes the currently-running job + everything
    # waiting. Reject when the queue is full so users get a 503 instead of
    # piling up an unbounded backlog. The setting name is legacy ("concurrent")
    # but its meaning is now "max queue depth". Admins bypass — they need to
    # be able to provision regardless of how clogged the queue is.
    if get_user_plan(user_id) != "admin" and queue_depth() >= settings.max_concurrent_provisions:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Provisioning queue is full, retry shortly",
        )

    # Subdomains live in a single global namespace (see sql/004); check before
    # we kick off the worker so the user gets an actionable 409 rather than a
    # silent failure later.
    taken = (
        admin_client()
        .table("sites")
        .select("id")
        .eq("subdomain", subdomain)
        .limit(1)
        .execute()
        .data
        or []
    )
    if taken:
        raise HTTPException(status.HTTP_409_CONFLICT, "subdomain is already taken")

    # Claim the subdomain on the row before enqueueing so concurrent /provision
    # calls collide on the unique index instead of racing the worker.
    try:
        admin_client().table("sites").update({"subdomain": subdomain}).eq("id", site_id).execute()
    except Exception as exc:  # likely unique-index collision
        raise HTTPException(status.HTTP_409_CONFLICT, "subdomain is already taken") from exc

    slug = slugify(Path(site.get("original_filename") or site.get("name") or "site").stem)
    zip_path = site_zip_path(settings.storage_root, user_id, site_id, display_name=slug)

    enqueue_provision(settings, site_id, user_id, zip_path, subdomain)

    return {"siteId": site_id, "status": STATUS_QUEUED, "subdomain": subdomain}


def _load_site_for_redeploy(site_id: str, user_id: str) -> tuple[dict, bool]:
    """Look up the site and assert the caller may redeploy it.

    Shared by /redeploy, /redeploy-zip, /redeploy-from-repo. Returns the row
    plus an `is_admin` flag so callers can apply the same rate-limit bypass.
    """
    is_admin = get_user_plan(user_id) == "admin"
    lookup = (
        admin_client()
        .table("sites")
        .select(
            "id, user_id, status, vmid, repo_host, repo_url, repo_branch, "
            "last_deploy_at, original_filename, name"
        )
        .eq("id", site_id)
    )
    if not is_admin:
        lookup = lookup.eq("user_id", user_id)
    rows = lookup.limit(1).execute().data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "site not found")
    site = rows[0]

    if not site.get("vmid"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "site has no CT yet (never went live) — provision it first",
        )
    # Only allow redeploys from terminal states. 'queued'/'updating'/
    # 'provisioning'/'scanning' all mean a worker is already touching the
    # row; stacking a second job risks racing the first.
    if site.get("status") not in (STATUS_LIVE, "failed"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"site cannot be redeployed in status={site.get('status')}",
        )
    return site, is_admin


def _enforce_redeploy_limits(site: dict, settings: Settings, is_admin: bool) -> None:
    """Per-site rate limit + global queue backpressure. Admins bypass both."""
    if not is_admin and settings.redeploy_rate_limit_seconds > 0:
        last = site.get("last_deploy_at")
        if last:
            try:
                last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - last_dt).total_seconds()
                if age < settings.redeploy_rate_limit_seconds:
                    raise HTTPException(
                        status.HTTP_429_TOO_MANY_REQUESTS,
                        f"Last redeploy was {int(age)}s ago; wait "
                        f"{int(settings.redeploy_rate_limit_seconds - age)}s.",
                    )
            except ValueError:
                # Unparseable timestamp — let it through rather than block on
                # a corrupt row.
                pass

    if not is_admin and queue_depth() >= settings.max_concurrent_provisions:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Provisioning queue is full, retry shortly",
        )


@router.post("/{site_id}/redeploy", status_code=status.HTTP_202_ACCEPTED)
async def redeploy_site(
    site_id: str,
    user_id: str = Depends(current_user_id),
    settings: Settings = Depends(get_settings),
):
    """One-click update: re-pull the site's stored repo and push the new
    contents into the existing CT. No new provisioning, no new subdomain.

    Requires the site to have a stored repo_url. Zip-uploaded sites must use
    /redeploy-zip or /redeploy-from-repo to update.

    Rate-limited per site to stop rapid re-clicks from stacking the queue.
    Admin bypasses both the ownership check and the rate limit.
    """
    site, is_admin = _load_site_for_redeploy(site_id, user_id)
    owner_id = site.get("user_id") or user_id

    if not site.get("repo_url"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "site was not uploaded from a git repo — upload a new zip or "
            "switch this site to a repo source instead",
        )

    _enforce_redeploy_limits(site, settings, is_admin)

    enqueue_redeploy(
        settings,
        site_id,
        owner_id,
        site["repo_host"],
        site["repo_url"],
        site.get("repo_branch"),
    )

    return {"siteId": site_id, "status": STATUS_QUEUED}


@router.post("/{site_id}/redeploy-zip", status_code=status.HTTP_202_ACCEPTED)
async def redeploy_site_from_zip(
    site_id: str,
    file: UploadFile = File(...),
    user_id: str = Depends(current_user_id),
    settings: Settings = Depends(get_settings),
):
    """Update a live site by uploading a fresh zip. CT, subdomain, and gateway
    route stay put — we just re-scan the new bundle and push it into the
    existing vmid.

    Works for both zip-uploaded and repo-uploaded sites. Same security pipeline
    as /upload + /provision: zip policy → php-malware-finder → clamav, then
    redeploy ansible against the existing CT.
    """
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "File must be a .zip")

    site, is_admin = _load_site_for_redeploy(site_id, user_id)
    owner_id = site.get("user_id") or user_id

    _enforce_redeploy_limits(site, settings, is_admin)

    # Per-plan storage cap still applies — a redeploy can grow the site.
    plan_limits = get_user_plan_limits(owner_id)

    # Overwrite the existing zip in place (same slug + site_id). Worker will
    # pick up whatever bytes are on disk under that path.
    slug = slugify(Path(site.get("original_filename") or site.get("name") or "site").stem)
    target = site_zip_path(settings.storage_root, owner_id, site_id, display_name=slug)

    # Write to a sibling temp file first so a partial/oversized upload doesn't
    # clobber the working zip the site was last deployed from. Swap on success.
    tmp_target = target.with_suffix(".zip.partial")
    written = 0
    try:
        with tmp_target.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > settings.max_upload_bytes:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        "File exceeds 100 MB",
                    )
                out.write(chunk)
    except HTTPException:
        tmp_target.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp_target.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to store uploaded file",
        ) from exc

    try:
        assert_within_storage(owner_id, plan_limits, written)
    except HTTPException:
        tmp_target.unlink(missing_ok=True)
        raise

    tmp_target.replace(target)

    # Update size_bytes so the dashboard storage bar reflects the new bundle.
    try:
        admin_client().table("sites").update(
            {"size_bytes": written, "original_filename": file.filename}
        ).eq("id", site_id).execute()
    except Exception:
        log.exception("could not persist new size for site_id=%s", site_id)

    enqueue_zip_redeploy(settings, site_id, owner_id, target)

    return {"siteId": site_id, "status": STATUS_QUEUED}


@router.post("/{site_id}/redeploy-from-repo", status_code=status.HTTP_202_ACCEPTED)
async def redeploy_site_from_repo(
    site_id: str,
    payload: dict = Body(...),
    user_id: str = Depends(current_user_id),
    settings: Settings = Depends(get_settings),
):
    """Update a live site from a (possibly new) git repo URL. Persists the
    repo_url/branch onto the row so future one-click /redeploy calls work
    without re-supplying it, then enqueues the standard repo redeploy flow.

    This is how a zip-uploaded site "switches" to a git source, or how a
    repo-uploaded site swaps to a different repo.
    """
    repo_url = (payload.get("repoUrl") or payload.get("repo_url") or "").strip()
    branch = (payload.get("branch") or "").strip() or None
    if not repo_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "repoUrl is required")

    site, is_admin = _load_site_for_redeploy(site_id, user_id)
    owner_id = site.get("user_id") or user_id

    try:
        host, _owner, _repo = validate_repo_url(repo_url)
        cleaned_branch = validate_branch(branch)
    except RepoCloneError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    _enforce_redeploy_limits(site, settings, is_admin)

    # Persist the new source on the row so subsequent one-click redeploys
    # don't need the payload again.
    try:
        admin_client().table("sites").update(
            {
                "repo_host": host,
                "repo_url": repo_url,
                "repo_branch": cleaned_branch,
            }
        ).eq("id", site_id).execute()
    except Exception as exc:
        log.exception("could not persist repo source for site_id=%s", site_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to update site source",
        ) from exc

    enqueue_redeploy(settings, site_id, owner_id, host, repo_url, cleaned_branch)

    return {"siteId": site_id, "status": STATUS_QUEUED}


@router.delete("/{site_id}", status_code=status.HTTP_200_OK)
async def delete_site(
    site_id: str,
    user_id: str = Depends(current_user_id),
    settings: Settings = Depends(get_settings),
):
    """Remove a site: drop the DB row immediately, then tear down CT + gateway
    route + zip asynchronously. UI sees the row vanish instantly; the heavy
    work happens in the background.
    """
    # Look up before delete so we can pass subdomain/zip path to the teardown.
    # `.eq("user_id", user_id)` enforces ownership at the API layer in addition
    # to whatever RLS does in Postgres — defense in depth. Admins bypass the
    # ownership filter so they can clean up other users' sites from the dash.
    is_admin = get_user_plan(user_id) == "admin"
    lookup = (
        admin_client()
        .table("sites")
        .select("id, user_id, subdomain, original_filename, name")
        .eq("id", site_id)
    )
    if not is_admin:
        lookup = lookup.eq("user_id", user_id)
    rows = lookup.limit(1).execute().data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "site not found")
    site = rows[0]
    owner_id = site.get("user_id") or user_id

    # Reconstruct the zip path the upload endpoint wrote to. We don't store
    # the absolute path in the DB, but the layout is deterministic. Use the
    # site's actual owner — for an admin deleting someone else's site, the
    # zip lives under that user's storage tree, not the admin's.
    slug = slugify(Path(site.get("original_filename") or site.get("name") or "site").stem)
    try:
        zip_path = site_zip_path(settings.storage_root, owner_id, site_id, display_name=slug)
    except Exception:
        log.exception("could not derive zip path for site_id=%s; teardown will skip the file", site_id)
        zip_path = None

    # Delete the row first so the dashboard reflects the action immediately.
    # If teardown later fails, we'll have an orphan CT — preferable to leaving
    # the row in a "deleting" limbo when the user clicked a destructive button.
    delete_q = admin_client().table("sites").delete().eq("id", site_id)
    if not is_admin:
        delete_q = delete_q.eq("user_id", user_id)
    delete_q.execute()

    enqueue_teardown(
        settings,
        site_id,
        site.get("subdomain"),
        zip_path,
    )

    return {"siteId": site_id, "status": "deleting"}
