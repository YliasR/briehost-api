"""Safe zip handling: per-user paths, readable filenames, and path-traversal-resistant extraction."""
import os
import re
import zipfile
from pathlib import Path


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str, max_len: int = 40) -> str:
    """Lowercase, hyphen-separated, ascii-only slug. Empty input falls back to 'site'."""
    value = (value or "").lower()
    value = _SLUG_RE.sub("-", value).strip("-")
    return (value[:max_len].rstrip("-")) or "site"


def site_zip_path(storage_root: str, user_id: str, site_id: str, display_name: str | None = None) -> Path:
    """Return the on-disk zip path. Filename is `<slug>-<site_id>.zip` for human-readable listings."""
    user_dir = Path(storage_root) / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(display_name) if display_name else "site"
    return user_dir / f"{slug}-{site_id}.zip"


class UnsafeZipError(ValueError):
    pass


def validate_zip_policy(
    zip_path: Path,
    *,
    max_files: int,
    max_uncompressed_bytes: int,
    max_compression_ratio: int,
) -> None:
    """Reject zip bombs / oversized archives before extraction."""
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > max_files:
            raise UnsafeZipError(f"Too many files in zip: {len(infos)} > {max_files}")

        total_uncompressed = 0
        total_compressed = 0
        for member in infos:
            total_uncompressed += member.file_size
            total_compressed += member.compress_size
            if total_uncompressed > max_uncompressed_bytes:
                raise UnsafeZipError(
                    f"Uncompressed size exceeds limit: {total_uncompressed} > {max_uncompressed_bytes}"
                )

        if total_compressed > 0:
            ratio = total_uncompressed / total_compressed
            if ratio > max_compression_ratio:
                raise UnsafeZipError(f"Compression ratio {ratio:.1f} exceeds {max_compression_ratio}")


def safe_extract(zip_path: Path, dest_dir: Path) -> None:
    """Extract `zip_path` into `dest_dir`, rejecting absolute paths, `..` segments, and symlinks."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_real = os.path.realpath(dest_dir)

    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise UnsafeZipError(f"Symlink not allowed: {member.filename}")

            target = os.path.realpath(os.path.join(dest_real, member.filename))
            if not (target == dest_real or target.startswith(dest_real + os.sep)):
                raise UnsafeZipError(f"Path traversal blocked: {member.filename}")

        zf.extractall(dest_dir)
