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

# Discover the vhost nginx actually has enabled. The golden template ships with
# `sites-enabled/site.conf` (not `default`), so hard-coding `default` writes a
# file nginx never reads. Follow whichever symlink is live; fall back to
# `default` only if nothing is enabled.
NGINX_VHOST=""
for link in /etc/nginx/sites-enabled/*; do
    [[ -L "$link" ]] || continue
    target="$(readlink -f "$link")"
    if [[ -n "$target" ]]; then
        NGINX_VHOST="$target"
        break
    fi
done
if [[ -z "$NGINX_VHOST" ]]; then
    NGINX_VHOST="/etc/nginx/sites-available/default"
    ln -sf "$NGINX_VHOST" /etc/nginx/sites-enabled/default
fi

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

    # Bootstrap .env. Laravel's normal workflow is to copy .env.example, but
    # plenty of zips ship without one (gitignored, packaged from a clean
    # source tree, etc). Without .env, Laravel falls back to whatever is
    # baked into bootstrap/cache/config.php — which for shipped projects is
    # almost always the original mysql connection. So if neither file exists,
    # synthesize a minimal .env that key:generate + set_env_var can fill in.
    if [[ ! -f "$WEB_ROOT/.env" ]]; then
        if [[ -f "$WEB_ROOT/.env.example" ]]; then
            cp "$WEB_ROOT/.env.example" "$WEB_ROOT/.env"
        else
            cat > "$WEB_ROOT/.env" <<'ENV'
APP_NAME=Brieblast
APP_ENV=production
APP_KEY=
APP_DEBUG=false
APP_URL=http://localhost

LOG_CHANNEL=stack
LOG_LEVEL=error

DB_CONNECTION=sqlite

SESSION_DRIVER=database
SESSION_LIFETIME=120

CACHE_STORE=database
QUEUE_CONNECTION=database
ENV
        fi
        chown "$WEB_USER:$WEB_USER" "$WEB_ROOT/.env"
    fi

    # Install dependencies as www-data so the resulting vendor/ tree is owned
    # correctly. --no-dev keeps the install lean; --no-interaction prevents
    # composer from blocking on prompts.
    # Minimal LXC images don't ship sudo — use runuser (util-linux, always present).
    if [[ ! -d "$WEB_ROOT/vendor" ]]; then
        runuser -u "$WEB_USER" -- \
            env COMPOSER_ALLOW_SUPERUSER=0 \
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
        runuser -u "$WEB_USER" -- \
            php "$WEB_ROOT/artisan" key:generate --force --no-interaction
    fi

    # Force the DB connection to sqlite — no per-tenant MySQL daemon means the
    # default DB_CONNECTION=mysql in shipped .env.example files would explode
    # on the first DB-touching route. SQLite is a single file under storage/.
    # set_env_var <KEY> <VALUE>: rewrite if present, append if missing.
    set_env_var() {
        local key="$1"
        local value="$2"
        if grep -qE "^${key}=" "$WEB_ROOT/.env"; then
            sed -i "s|^${key}=.*|${key}=${value}|" "$WEB_ROOT/.env"
        else
            echo "${key}=${value}" >> "$WEB_ROOT/.env"
        fi
    }
    SQLITE_PATH="$WEB_ROOT/database/database.sqlite"
    mkdir -p "$WEB_ROOT/database"
    touch "$SQLITE_PATH"
    set_env_var DB_CONNECTION sqlite
    set_env_var DB_DATABASE "$SQLITE_PATH"
    # Blank out MySQL-specific keys so Laravel doesn't try to honor them.
    for k in DB_HOST DB_PORT DB_USERNAME DB_PASSWORD; do
        if grep -qE "^${k}=" "$WEB_ROOT/.env"; then
            sed -i "s|^${k}=.*|${k}=|" "$WEB_ROOT/.env"
        fi
    done

    # Laravel needs to write to storage/, bootstrap/cache/, and the sqlite
    # file at runtime. 0775 + group www-data is the conventional permission.
    if [[ -d "$WEB_ROOT/storage" ]]; then
        chown -R "$WEB_USER:$WEB_USER" "$WEB_ROOT/storage"
        chmod -R ug+rwX "$WEB_ROOT/storage"
    fi
    if [[ -d "$WEB_ROOT/bootstrap/cache" ]]; then
        chown -R "$WEB_USER:$WEB_USER" "$WEB_ROOT/bootstrap/cache"
        chmod -R ug+rwX "$WEB_ROOT/bootstrap/cache"
    fi
    chown -R "$WEB_USER:$WEB_USER" "$WEB_ROOT/database"
    chmod -R ug+rwX "$WEB_ROOT/database"
    chown "$WEB_USER:$WEB_USER" "$WEB_ROOT/.env"

    # Clear cached config/routes/views BEFORE migrate. Many zips ship with a
    # populated bootstrap/cache/config.php (left over from local `artisan
    # config:cache` or a CI pipeline) — that snapshot holds the original
    # mysql connection settings and ignores everything we just wrote to .env.
    # Result without this step: Laravel still tries DB_CONNECTION=mysql,
    # migrate fails with "could not find driver", site boots into a 500 loop.
    for cmd in config:clear cache:clear route:clear view:clear; do
        runuser -u "$WEB_USER" -- \
            php "$WEB_ROOT/artisan" "$cmd" --no-interaction || true
    done

    # Run migrations against the sqlite file. Schema is empty on a fresh deploy,
    # so this populates tables defined in database/migrations/. Failures here
    # shouldn't kill the whole deploy — some apps may not ship migrations.
    if [[ -d "$WEB_ROOT/database/migrations" ]]; then
        runuser -u "$WEB_USER" -- \
            php "$WEB_ROOT/artisan" migrate --force --no-interaction || \
            echo "WARNING: artisan migrate failed — site will boot but DB-backed features may not work." >&2
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
