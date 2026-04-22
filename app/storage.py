"""Safe zip handling: per-user paths and path-traversal-resistant extraction."""
import os
import zipfile
from pathlib import Path


def site_zip_path(storage_root: str, user_id: str, site_id: str) -> Path:
    user_dir = Path(storage_root) / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir / f"{site_id}.zip"


class UnsafeZipError(ValueError):
    pass


def safe_extract(zip_path: Path, dest_dir: Path) -> None:
    """Extract `zip_path` into `dest_dir`, rejecting absolute paths, `..` segments, and symlinks."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_real = os.path.realpath(dest_dir)

    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            # Reject symlinks (Unix mode bits in external_attr)
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise UnsafeZipError(f"Symlink not allowed: {member.filename}")

            target = os.path.realpath(os.path.join(dest_real, member.filename))
            if not (target == dest_real or target.startswith(dest_real + os.sep)):
                raise UnsafeZipError(f"Path traversal blocked: {member.filename}")

        zf.extractall(dest_dir)
