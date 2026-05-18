import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routes import payments, sites
from app.sync_gateway import resync as resync_gateway
from app.worker import start_consumer, stop_consumer

log = logging.getLogger("briehost.main")

app = FastAPI(title="briehost-api")

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(sites.router)
app.include_router(payments.router)


@app.on_event("startup")
def _resync_gateway_on_boot() -> None:
    # Best-effort: if the gateway is unreachable (rebooting, not yet provisioned)
    # the API still starts. Operators can re-run `python -m app.sync_gateway`.
    try:
        resync_gateway(settings)
    except Exception:
        log.exception("gateway resync on startup failed; continuing")


@app.on_event("startup")
async def _start_provision_consumer() -> None:
    # Single FIFO consumer for provision + teardown jobs. Must be created
    # inside the event loop, so this is async and lives in startup.
    await start_consumer(settings)


@app.on_event("shutdown")
async def _stop_provision_consumer() -> None:
    await stop_consumer()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
