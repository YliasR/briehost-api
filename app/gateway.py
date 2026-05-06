"""Caddy gateway integration.

When a site goes live, the worker pushes a host->upstream route to the Caddy
admin API on the gateway CT. Caddy holds the slug -> tenant IP mapping in
memory; Supabase remains the durable source of truth (so a gateway reboot can
re-hydrate by replaying live rows).

See CLOUDFLARE_GATEWAY_SETUP.md for the gateway topology and Caddyfile.
"""
from __future__ import annotations

import logging
import re

import httpx

from app.config import Settings

log = logging.getLogger("briehost.gateway")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MAX_SUBDOMAIN_LEN = 40

# Caddy admin path that contains the dynamic per-site routes.
_ROUTES_PATH = "/config/apps/http/servers/srv0/routes"


def derive_subdomain(seed: str) -> str:
    """Lowercase, replace non-alphanumerics with '-', collapse, trim, cap length.

    Returns "" for empty/garbage input — caller decides what to do.
    """
    slug = _SLUG_RE.sub("-", seed.lower()).strip("-")
    return slug[:_MAX_SUBDOMAIN_LEN]


def _route_id(subdomain: str) -> str:
    return f"site:{subdomain}"


def register_route(settings: Settings, subdomain: str, ip: str) -> None:
    """Upsert the host -> ip route on the gateway. Raises on transport/HTTP errors.

    Caller treats this as best-effort: a healthy site must not be flipped to
    `failed` because the gateway was unreachable.
    """
    if not settings.gateway_caddy_admin_url or not settings.gateway_domain:
        log.info("gateway disabled (admin url or domain unset); skipping %s", subdomain)
        return

    base = settings.gateway_caddy_admin_url.rstrip("/")
    timeout = settings.gateway_request_timeout_seconds
    rid = _route_id(subdomain)
    host = f"{subdomain}.{settings.gateway_domain}"
    body = {
        "@id": rid,
        "match": [{"host": [host]}],
        "handle": [
            {
                "handler": "reverse_proxy",
                "upstreams": [{"dial": f"{ip}:80"}],
            }
        ],
        "terminal": True,
    }

    with httpx.Client(timeout=timeout) as client:
        # Drop any prior route with the same id (re-provision, IP change).
        # Caddy returns 404 if the id is unknown — that's fine.
        resp = client.delete(f"{base}/id/{rid}")
        if resp.status_code not in (200, 404):
            resp.raise_for_status()

        # PUT inserts before the given index and shifts existing elements down,
        # which puts the new route ahead of the Caddyfile catchall at index 0.
        # POST on an array index path appends instead — wrong for our ordering.
        resp = client.put(f"{base}{_ROUTES_PATH}/0", json=body)
        resp.raise_for_status()


def _build_route(subdomain: str, ip: str, domain: str) -> dict:
    return {
        "@id": _route_id(subdomain),
        "match": [{"host": [f"{subdomain}.{domain}"]}],
        "handle": [
            {
                "handler": "reverse_proxy",
                "upstreams": [{"dial": f"{ip}:80"}],
            }
        ],
        "terminal": True,
    }


def replace_all_routes(settings: Settings, sites: list[tuple[str, str]]) -> int:
    """Re-hydrate srv0's per-site routes from the given (subdomain, ip) pairs.

    Strategy: enumerate existing `site:*` routes and DELETE each by id, then
    POST the desired set fresh. Static routes (e.g. the catchall in the
    Caddyfile) are untouched because they don't carry a `site:` id.

    Uses only GET / DELETE / POST — the same verbs as the per-provision path —
    so we don't depend on PATCH-on-array semantics. Returns the count pushed.
    """
    if not settings.gateway_caddy_admin_url or not settings.gateway_domain:
        log.info("gateway disabled; skipping replace_all_routes")
        return 0

    base = settings.gateway_caddy_admin_url.rstrip("/")
    timeout = settings.gateway_request_timeout_seconds

    with httpx.Client(timeout=timeout) as client:
        resp = client.get(f"{base}{_ROUTES_PATH}")
        resp.raise_for_status()
        existing = resp.json() or []

        stale_ids = [
            str(r["@id"]) for r in existing if str(r.get("@id", "")).startswith("site:")
        ]
        for rid in stale_ids:
            r = client.delete(f"{base}/id/{rid}")
            if r.status_code not in (200, 404):
                r.raise_for_status()

        pushed = 0
        for sub, ip in sites:
            if not (sub and ip):
                continue
            body = _build_route(sub, ip, settings.gateway_domain)
            r = client.put(f"{base}{_ROUTES_PATH}/0", json=body)
            r.raise_for_status()
            pushed += 1

    return pushed


def unregister_route(settings: Settings, subdomain: str) -> None:
    """Remove the route for a deprovisioned site. Best-effort, idempotent."""
    if not settings.gateway_caddy_admin_url:
        return
    base = settings.gateway_caddy_admin_url.rstrip("/")
    rid = _route_id(subdomain)
    with httpx.Client(timeout=settings.gateway_request_timeout_seconds) as client:
        resp = client.delete(f"{base}/id/{rid}")
        if resp.status_code not in (200, 404):
            resp.raise_for_status()
