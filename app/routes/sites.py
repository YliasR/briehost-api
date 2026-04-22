"""Site upload endpoint."""
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.auth import current_user_id
from app.config import Settings, get_settings
from app.db import admin_client
from app.storage import site_zip_path

router = APIRouter(prefix="/api/sites", tags=["sites"])


@router.post("/upload")
async def upload_site(
    file: UploadFile = File(...),
    user_id: str = Depends(current_user_id),
    settings: Settings = Depends(get_settings),
):
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "File must be a .zip")

    site_id = str(uuid.uuid4())
    target = site_zip_path(settings.storage_root, user_id, site_id)

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

    name = Path(file.filename).stem or "site"
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

    # TODO: enqueue background task -> safe_extract -> provision_php_site -> update status
    # For now the dashboard will see status = 'uploaded' and the team can wire provisioning next.

    return {"siteId": site_id, "status": "uploaded"}
