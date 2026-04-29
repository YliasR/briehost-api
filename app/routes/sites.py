"""Site upload endpoint."""
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status

from app.auth import current_user_id
from app.config import Settings, get_settings
from app.db import admin_client
from app.storage import site_zip_path, slugify
from app.worker import STATUS_UPLOADED, enqueue_provision, inflight_count

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
