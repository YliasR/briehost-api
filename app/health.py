"""Runtime health checks for provisioned sites."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.db import admin_client

log = logging.getLogger("briehost.health")

_task: asyncio.Task | None = None


@dataclass(frozen=True)
class SiteTarget:
    id: str
    subdomain: str | None
    ip_address: str | None


def _public_url(settings: Settings, site: SiteTarget) -> str | None:
    if site.subdomain and settings.gateway_domain:
        return f"{settings.healthcheck_public_scheme}://{site.subdomain}.{settings.gateway_domain}/"
    if site.ip_address:
        return f"http://{site.ip_address}/"
    return None


def _fetch_live_sites() -> list[SiteTarget]:
    rows = (
        admin_client()
        .table("sites")
        .select("id, subdomain, ip_address")
        .eq("status", "live")
        .execute()
        .data
        or []
    )
    return [
        SiteTarget(
            id=str(row["id"]),
            subdomain=row.get("subdomain"),
            ip_address=row.get("ip_address"),
        )
        for row in rows
    ]


def record_health_check(
    site_id: str,
    status: str,
    response_ms: int | None = None,
    status_code: int | None = None,
    error_message: str | None = None,
) -> None:
    params: dict[str, Any] = {
        "p_site_id": site_id,
        "p_status": status,
        "p_response_ms": response_ms,
        "p_status_code": status_code,
        "p_error_message": error_message,
    }
    admin_client().rpc("record_site_health_check", params).execute()


async def _check_site(client: httpx.AsyncClient, settings: Settings, site: SiteTarget) -> None:
    url = _public_url(settings, site)
    if not url:
        await asyncio.to_thread(
            record_health_check,
            site.id,
            "unknown",
            None,
            None,
            "No subdomain/gateway domain or IP address available for health check",
        )
        return

    started = time.perf_counter()
    try:
        response = await client.get(url, follow_redirects=True)
        response_ms = int((time.perf_counter() - started) * 1000)
        status = "up" if 100 <= response.status_code < 500 else "down"
        await asyncio.to_thread(
            record_health_check,
            site.id,
            status,
            response_ms,
            response.status_code,
            None if status == "up" else f"HTTP {response.status_code}",
        )
    except httpx.TimeoutException as exc:
        await asyncio.to_thread(
            record_health_check,
            site.id,
            "down",
            int((time.perf_counter() - started) * 1000),
            None,
            f"timeout: {exc}",
        )
    except httpx.HTTPError as exc:
        await asyncio.to_thread(
            record_health_check,
            site.id,
            "down",
            int((time.perf_counter() - started) * 1000),
            None,
            str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - health checks should never kill the monitor
        log.exception("health check crashed for site_id=%s", site.id)
        await asyncio.to_thread(
            record_health_check,
            site.id,
            "unknown",
            int((time.perf_counter() - started) * 1000),
            None,
            f"health check crash: {exc}",
        )


async def run_once(settings: Settings) -> None:
    sites = await asyncio.to_thread(_fetch_live_sites)
    if not sites:
        return

    limits = httpx.Limits(max_connections=settings.healthcheck_max_concurrency)
    timeout = httpx.Timeout(settings.healthcheck_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        semaphore = asyncio.Semaphore(settings.healthcheck_max_concurrency)

        async def guarded(site: SiteTarget) -> None:
            async with semaphore:
                await _check_site(client, settings, site)

        await asyncio.gather(*(guarded(site) for site in sites))


async def _monitor_loop(settings: Settings) -> None:
    log.info(
        "site health monitor started interval=%ss timeout=%ss",
        settings.healthcheck_interval_seconds,
        settings.healthcheck_timeout_seconds,
    )
    while True:
        try:
            await run_once(settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("site health monitor pass failed")
        await asyncio.sleep(settings.healthcheck_interval_seconds)


async def start_monitor(settings: Settings) -> None:
    global _task
    if not settings.healthcheck_enabled:
        log.info("site health monitor disabled")
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_monitor_loop(settings), name="site-health-monitor")


async def stop_monitor() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
