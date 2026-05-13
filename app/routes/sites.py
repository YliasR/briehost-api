"""Site upload + teardown endpoints."""
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status

from app.auth import current_user_id
from app.config import Settings, get_settings
from app.db import admin_client
from app.storage import site_zip_path, slugify
from app.worker import STATUS_UPLOADED, enqueue_provision, enqueue_teardown, inflight_count

log = logging.getLogger("briehost.routes.sites")

router = APIRouter(prefix="/api/sites", tags=["sites"])


@router.post("/upload")
async def upload_site(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user_id: str = Depends(current_user_id),
    settings: Settings = Depends(get_settings),
):
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "File must be a .zip")

    # Backpressure: in-process provisioning runs in BackgroundTasks; reject early
    # when at capacity instead of letting threads pile up. Real fix is a queue.
    if inflight_count() >= settings.max_concurrent_provisions:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Provisioning capacity reached, retry shortly",
        )

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

    enqueue_provision(background_tasks, settings, site_id, user_id, target)

    return {"siteId": site_id, "status": STATUS_UPLOADED}


@router.delete("/{site_id}", status_code=status.HTTP_200_OK)
async def delete_site(
    site_id: str,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(current_user_id),
    settings: Settings = Depends(get_settings),
):
    """Remove a site: drop the DB row immediately, then tear down CT + gateway
    route + zip asynchronously. UI sees the row vanish instantly; the heavy
    work happens in the background.
    """
    # Look up before delete so we can pass subdomain/zip path to the teardown.
    # `.eq("user_id", user_id)` enforces ownership at the API layer in addition
    # to whatever RLS does in Postgres — defense in depth.
    rows = (
        admin_client()
        .table("sites")
        .select("id, user_id, subdomain, original_filename, name")
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

    # Reconstruct the zip path the upload endpoint wrote to. We don't store
    # the absolute path in the DB, but the layout is deterministic.
    slug = slugify(Path(site.get("original_filename") or site.get("name") or "site").stem)
    try:
        zip_path = site_zip_path(settings.storage_root, user_id, site_id, display_name=slug)
    except Exception:
        log.exception("could not derive zip path for site_id=%s; teardown will skip the file", site_id)
        zip_path = None

    # Delete the row first so the dashboard reflects the action immediately.
    # If teardown later fails, we'll have an orphan CT — preferable to leaving
    # the row in a "deleting" limbo when the user clicked a destructive button.
    admin_client().table("sites").delete().eq("id", site_id).eq("user_id", user_id).execute()

    enqueue_teardown(
        background_tasks,
        settings,
        site_id,
        site.get("subdomain"),
        zip_path,
    )

    return {"siteId": site_id, "status": "deleting"}
