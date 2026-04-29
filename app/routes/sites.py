"""Site upload endpoint."""
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status

from app.auth import current_user_id
from app.config import Settings, get_settings
from app.db import admin_client
from app.storage import site_zip_path, slugify
from app.worker import enqueue_provision

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
            "status": "uploaded",
        }
    ).execute()

    # NOTE: provisioning currently runs in-process via FastAPI BackgroundTasks.
    # Long-running scans + ansible-playbook can saturate the worker pool under
    # concurrent uploads; move enqueue_provision to Celery/RQ/Arq before scaling.
    enqueue_provision(background_tasks, settings, site_id, user_id, target)

    return {"siteId": site_id, "status": "uploaded"}
