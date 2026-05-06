"""Re-hydrate the Caddy gateway from Supabase.

Caddy holds dynamic routes in memory; a gateway reboot drops them. This module
walks all `status='live'` rows that have both a subdomain and an ip_address,
and PATCHes the full route set into srv0 in one call.

Used in two places:
  - app/main.py startup hook — runs on every API CT boot.
  - CLI: `python -m app.sync_gateway` — manual replay after a gateway reboot.

Safe to run repeatedly: replace_all_routes is idempotent (it patches the whole
array), and it preserves any non-`site:*` routes already present in srv0
(notably the static catchall in the Caddyfile).
"""
from __future__ import annotations

import logging
import sys

from app.config import Settings, get_settings
from app.db import admin_client
from app.gateway import replace_all_routes

log = logging.getLogger("briehost.sync_gateway")


def resync(settings: Settings) -> int:
    """Push all live sites into the gateway. Returns count pushed."""
    if not settings.gateway_caddy_admin_url or not settings.gateway_domain:
        log.info("gateway disabled; skipping resync")
        return 0

    resp = (
        admin_client()
        .table("sites")
        .select("subdomain,ip_address")
        .eq("status", "live")
        .not_.is_("subdomain", "null")
        .not_.is_("ip_address", "null")
        .execute()
    )
    rows = resp.data or []
    pairs = [(r["subdomain"], r["ip_address"]) for r in rows]
    pushed = replace_all_routes(settings, pairs)
    log.info("gateway resync pushed %d route(s)", pushed)
    return pushed


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        resync(get_settings())
    except Exception:
        log.exception("gateway resync failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
