"""Site upload + teardown endpoints."""
import logging
import uuid
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
from app.worker import STATUS_QUEUED, STATUS_UPLOADED, enqueue_provision, enqueue_teardown, queue_depth

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
