"""Clone a public Git repository and pack it into a site zip.

Phase 1: only public HTTPS GitHub URLs. Private repos / other hosts would
need credential storage (App or per-user PAT) and a broader allowlist —
deliberately out of scope here.

The output zip has the same shape and on-disk path as a user-uploaded
zip, so the existing scan + provision pipeline can consume it unchanged.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("briehost.repo")


class RepoCloneError(ValueError):
    """Raised for any validation / clone / packaging failure surfaced to the user."""


_ALLOWED_HOSTS = {"github.com"}
# Conservative: owner / repo segments are alnum + `-`, `_`, `.`. Anything else
# is either a route segment (issues/, pulls/) or a typo — reject early.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Branches accept the same charset plus `/`. Disallow `..` and leading `-`
# so the value can't be mistaken for a flag by git.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


@dataclass
class ClonedRepo:
    owner: str
    repo: str
    ref: str  # branch or "default"
    display_name: str  # for original_filename column


def validate_repo_url(raw_url: str) -> tuple[str, str]:
    """Validate a public GitHub URL and return (owner, repo). Raises RepoCloneError."""
    owner, repo, _ = _normalize_github_url(raw_url)
    return owner, repo


def validate_branch(branch: str | None) -> str | None:
    """Public alias of _validate_branch for callers outside this module."""
    return _validate_branch(branch)


def _normalize_github_url(raw_url: str) -> tuple[str, str, str]:
    """Validate + canonicalize a public GitHub URL. Returns (owner, repo, clone_url)."""
    if not raw_url or not isinstance(raw_url, str):
        raise RepoCloneError("repo URL is required")

    candidate = raw_url.strip()
    if not candidate:
        raise RepoCloneError("repo URL is required")

    parsed = urlparse(candidate)
    if parsed.scheme != "https":
        raise RepoCloneError("only https:// repo URLs are accepted")
    if parsed.username or parsed.password:
        raise RepoCloneError("credentials in the URL are not allowed")
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise RepoCloneError(f"only public GitHub repos are supported (host={host or 'missing'})")
    if parsed.port not in (None, 443):
        raise RepoCloneError("non-default ports are not allowed")

    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2:
        raise RepoCloneError("URL must point at a repo, e.g. https://github.com/owner/repo")
    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not _SEGMENT_RE.match(owner) or not _SEGMENT_RE.match(repo):
        raise RepoCloneError("invalid owner or repository name")
    if repo in {".", ".."}:
        raise RepoCloneError("invalid repository name")

    clone_url = f"https://github.com/{owner}/{repo}.git"
    return owner, repo, clone_url


def _validate_branch(branch: str | None) -> str | None:
    if branch is None:
        return None
    cleaned = branch.strip()
    if not cleaned:
        return None
    if len(cleaned) > 200:
        raise RepoCloneError("branch name is too long")
    if cleaned.startswith("-"):
        raise RepoCloneError("branch name cannot start with '-'")
    if ".." in cleaned:
        raise RepoCloneError("branch name cannot contain '..'")
    if not _BRANCH_RE.match(cleaned):
        raise RepoCloneError("branch name contains unsupported characters")
    return cleaned


def _run_git_clone(clone_url: str, branch: str | None, dest: Path, timeout: int) -> None:
    cmd = [
        "git",
        "-c",
        "credential.helper=",  # never prompt, never write credentials
        "-c",
        "core.askPass=",  # belt-and-braces: no interactive auth
        "clone",
        "--depth",
        "1",
        "--single-branch",
        "--no-tags",
        "--config",
        "advice.detachedHead=false",
    ]
    if branch:
        cmd += ["--branch", branch]
    cmd += ["--", clone_url, str(dest)]

    env = {
        "GIT_TERMINAL_PROMPT": "0",  # fail instead of prompting for a password
        "GIT_ASKPASS": "/bin/echo",
        "GIT_LFS_SKIP_SMUDGE": "1",  # don't auto-fetch LFS blobs
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }

    log.info("cloning %s (branch=%s) into %s", clone_url, branch or "<default>", dest)
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RepoCloneError(f"clone timed out after {timeout}s") from exc

    if proc.returncode != 0:
        # git writes auth / not-found errors to stderr — surface a trimmed
        # version so the user can fix the URL, but never echo back stdout
        # (could contain unexpected content if a misconfigured server replies).
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["git clone failed"]
        message = tail[0][:200]
        raise RepoCloneError(f"git clone failed: {message}")


def _tree_stats(root: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes), skipping `.git/`."""
    total_bytes = 0
    file_count = 0
    for path in root.rglob("*"):
        # Skip the git metadata — it's stripped before zipping anyway.
        if ".git" in path.relative_to(root).parts:
            continue
        if path.is_symlink():
            raise RepoCloneError(f"repo contains a symlink ({path.relative_to(root)}); not allowed")
        if path.is_file():
            file_count += 1
            try:
                total_bytes += path.stat().st_size
            except OSError:
                continue
    return file_count, total_bytes


def _zip_tree(root: Path, target_zip: Path) -> int:
    """Pack `root` (minus `.git/`) into `target_zip`. Returns the resulting size."""
    target_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if ".git" in rel.parts:
                continue
            if path.is_symlink():
                # _tree_stats already rejected this, but defend against a TOCTOU
                # symlink appearing between the stat scan and the zip write.
                raise RepoCloneError(f"repo contains a symlink ({rel}); not allowed")
            if path.is_dir():
                continue
            zf.write(path, arcname=str(rel).replace("\\", "/"))
    return target_zip.stat().st_size


def clone_and_pack(
    *,
    owner: str,
    repo: str,
    branch: str | None,
    target_zip: Path,
    timeout_seconds: int,
    max_files: int,
    max_bytes: int,
) -> tuple[ClonedRepo, int]:
    """Shallow-clone the given GitHub repo, enforce size limits, pack into `target_zip`.

    `owner`/`repo`/`branch` are assumed pre-validated (see validate_repo_url /
    validate_branch). Returns the metadata + the on-disk zip size. Cleans up
    the temp clone dir on every exit path. Caller is responsible for unlinking
    `target_zip` if it later decides to roll back the upload row.
    """
    # Re-validate even though the caller should have — defense in depth so this
    # function can't be misused with attacker-controlled values from another path.
    if not _SEGMENT_RE.match(owner) or not _SEGMENT_RE.match(repo):
        raise RepoCloneError("invalid owner or repository name")
    clone_url = f"https://github.com/{owner}/{repo}.git"
    cleaned_branch = _validate_branch(branch)

    with tempfile.TemporaryDirectory(prefix="briehost-clone-") as tmp:
        clone_dir = Path(tmp) / "repo"
        _run_git_clone(clone_url, cleaned_branch, clone_dir, timeout_seconds)

        file_count, total_bytes = _tree_stats(clone_dir)
        if file_count > max_files:
            raise RepoCloneError(
                f"repo has too many files: {file_count} > {max_files}"
            )
        if total_bytes > max_bytes:
            raise RepoCloneError(
                f"repo is too large: {total_bytes} bytes > {max_bytes}"
            )

        # Drop git metadata before packaging so the zip matches what a normal
        # user upload would look like.
        git_dir = clone_dir / ".git"
        if git_dir.is_dir():
            shutil.rmtree(git_dir, ignore_errors=True)

        zip_size = _zip_tree(clone_dir, target_zip)

    display = f"{owner}-{repo}"
    if cleaned_branch:
        display = f"{display}@{cleaned_branch}"

    return (
        ClonedRepo(
            owner=owner,
            repo=repo,
            ref=cleaned_branch or "default",
            display_name=f"{display}.zip",
        ),
        zip_size,
    )
