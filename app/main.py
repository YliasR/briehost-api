import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routes import sites
from app.sync_gateway import resync as resync_gateway

log = logging.getLogger("briehost.main")

app = FastAPI(title="briehost-api")

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(sites.router)


@app.on_event("startup")
def _resync_gateway_on_boot() -> None:
    # Best-effort: if the gateway is unreachable (rebooting, not yet provisioned)
    # the API still starts. Operators can re-run `python -m app.sync_gateway`.
    try:
        resync_gateway(settings)
    except Exception:
        log.exception("gateway resync on startup failed; continuing")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
