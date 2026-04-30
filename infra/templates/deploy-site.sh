#!/bin/bash
# Usage: deploy-site.sh /path/to/file.zip
#
# Runs INSIDE the tenant CT (installed at /usr/local/bin/deploy-site.sh in the
# golden PHP template). Wipes the docroot, unpacks the zip, flattens a single
# top-level folder if present (common when users zip a folder on Windows),
# and fixes ownership + permissions.
set -euo pipefail

ZIP_FILE="${1:-}"
WEB_ROOT="/var/www/html"
WEB_USER="www-data"

if [[ -z "$ZIP_FILE" ]]; then
    echo "No zip file provided." >&2
    exit 1
fi
if [[ ! -r "$ZIP_FILE" ]]; then
    echo "Zip file not readable: $ZIP_FILE" >&2
    exit 1
fi

# Stage in a tempdir so a half-broken unzip can't leave the docroot empty.
STAGE="$(mktemp -d -t briehost-deploy.XXXXXX)"
trap 'rm -rf -- "$STAGE"' EXIT

unzip -qq -o "$ZIP_FILE" -d "$STAGE"

# If the zip contained exactly one top-level entry and it's a directory,
# treat that directory's contents as the site root. Catches the very common
# Windows pattern of right-clicking a folder -> "Send to -> Compressed folder".
shopt -s dotglob nullglob
top=( "$STAGE"/* )
shopt -u dotglob nullglob

if [[ ${#top[@]} -eq 1 && -d "${top[0]}" ]]; then
    SRC="${top[0]}"
    echo "Detected single top-level folder '$(basename "$SRC")' — flattening."
else
    SRC="$STAGE"
fi

# Wipe docroot (including dotfiles) only after we know the unzip succeeded.
rm -rf -- "${WEB_ROOT:?}"/* "${WEB_ROOT:?}"/.[!.]* 2>/dev/null || true

shopt -s dotglob nullglob
mv -- "$SRC"/* "$WEB_ROOT"/
shopt -u dotglob nullglob

chown -R "$WEB_USER:$WEB_USER" "$WEB_ROOT"
find "$WEB_ROOT" -type d -exec chmod 755 {} +
find "$WEB_ROOT" -type f -exec chmod 644 {} +

echo "Deployment OK"
