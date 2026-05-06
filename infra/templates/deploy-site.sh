#!/bin/bash
# Usage: deploy-site.sh /path/to/file.zip
#
# Runs INSIDE the tenant CT (installed at /usr/local/bin/deploy-site.sh in the
# golden PHP template). Wipes the docroot, unpacks the zip, flattens a single
# top-level folder if present (common when users zip a folder on Windows),
# and fixes ownership + permissions.
#
# Detects Laravel apps (artisan + composer.json at the docroot) and adapts:
#   - nginx server block points at <docroot>/public with Laravel try_files
#   - composer install --no-dev runs as www-data
#   - .env is bootstrapped from .env.example if missing, APP_KEY generated
#   - storage/ and bootstrap/cache/ get group-write for www-data
# Static PHP sites get a plain server block rooted at /var/www/html.
set -euo pipefail

ZIP_FILE="${1:-}"
WEB_ROOT="/var/www/html"
WEB_USER="www-data"
NGINX_VHOST="/etc/nginx/sites-available/default"

# php-fpm socket varies by PHP version; pick whatever the template ships.
PHP_FPM_SOCK="$(ls /run/php/php*-fpm.sock 2>/dev/null | head -1)"
if [[ -z "$PHP_FPM_SOCK" ]]; then
    echo "No php-fpm socket found under /run/php/." >&2
    exit 1
fi

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

# ---------------------------------------------------------------------------
# Framework detection. Add more branches here as needed (Symfony, etc).
# ---------------------------------------------------------------------------

write_nginx_vhost() {
    # $1 = docroot, $2 = "laravel" | "static" — controls try_files behavior.
    local docroot="$1"
    local mode="$2"
    local try_files
    if [[ "$mode" == "laravel" ]]; then
        # Laravel sends every non-file URL through public/index.php.
        try_files='try_files $uri $uri/ /index.php?$query_string;'
    else
        # Static PHP: serve files directly, 404 otherwise. Index.php still
        # works because it's listed in `index` below.
        try_files='try_files $uri $uri/ =404;'
    fi
    cat > "$NGINX_VHOST" <<EOF
server {
    listen 80 default_server;
    listen [::]:80 default_server;

    root ${docroot};
    index index.php index.html index.htm;

    server_name _;

    location / {
        ${try_files}
    }

    location ~ \.php\$ {
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:${PHP_FPM_SOCK};
    }

    # Laravel + general hygiene: don't serve dotfiles like .env, .git, etc.
    location ~ /\. {
        deny all;
    }
}
EOF
}

if [[ -f "$WEB_ROOT/artisan" && -f "$WEB_ROOT/composer.json" && -d "$WEB_ROOT/public" ]]; then
    echo "Laravel project detected — running framework setup."

    # Bootstrap .env. Laravel's default workflow is to copy .env.example.
    if [[ ! -f "$WEB_ROOT/.env" && -f "$WEB_ROOT/.env.example" ]]; then
        cp "$WEB_ROOT/.env.example" "$WEB_ROOT/.env"
        chown "$WEB_USER:$WEB_USER" "$WEB_ROOT/.env"
    fi

    # Install dependencies as www-data so the resulting vendor/ tree is owned
    # correctly. --no-dev keeps the install lean; --no-interaction prevents
    # composer from blocking on prompts.
    if [[ ! -d "$WEB_ROOT/vendor" ]]; then
        sudo -u "$WEB_USER" \
            COMPOSER_ALLOW_SUPERUSER=0 \
            COMPOSER_HOME="$WEB_ROOT/.composer-home" \
            composer install \
                --working-dir="$WEB_ROOT" \
                --no-dev \
                --no-interaction \
                --optimize-autoloader \
                --prefer-dist
    else
        echo "vendor/ already present in zip — skipping composer install."
    fi

    # APP_KEY: required for sessions/encryption. Generate only if missing.
    if [[ -f "$WEB_ROOT/.env" ]] && ! grep -qE '^APP_KEY=base64:' "$WEB_ROOT/.env"; then
        sudo -u "$WEB_USER" php "$WEB_ROOT/artisan" key:generate --force --no-interaction
    fi

    # Laravel needs to write to storage/ and bootstrap/cache/ at runtime.
    # 0775 + group www-data is the conventional permission.
    if [[ -d "$WEB_ROOT/storage" ]]; then
        chown -R "$WEB_USER:$WEB_USER" "$WEB_ROOT/storage"
        chmod -R ug+rwX "$WEB_ROOT/storage"
    fi
    if [[ -d "$WEB_ROOT/bootstrap/cache" ]]; then
        chown -R "$WEB_USER:$WEB_USER" "$WEB_ROOT/bootstrap/cache"
        chmod -R ug+rwX "$WEB_ROOT/bootstrap/cache"
    fi

    write_nginx_vhost "$WEB_ROOT/public" laravel
else
    # Plain PHP / static site — make sure the vhost wasn't left pointing at
    # /public from a previous Laravel deploy on the same CT.
    write_nginx_vhost "$WEB_ROOT" static
fi

# Validate before reloading; nginx -t will refuse a broken config.
nginx -t

# Apply vhost changes. Reload (not restart) so existing connections drain.
systemctl reload nginx || systemctl restart nginx

# Clear opcache so the new files are picked up immediately. Service name
# tracks the PHP version (php8.2-fpm on Debian 12), so glob it.
for svc in $(systemctl list-units --type=service --no-legend 'php*-fpm.service' \
                | awk '{print $1}'); do
    systemctl reload "$svc" || true
done

echo "Deployment OK"
