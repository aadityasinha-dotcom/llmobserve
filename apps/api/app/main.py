import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import check_database_ready, dispose_engine
from app.routers import ingest, traces

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Surface a broken tenant-isolation setup in the startup logs rather than
    # waiting for the first readiness probe. Startup itself is not aborted: a
    # database that is merely slow to accept connections should not crash-loop
    # the service. /readyz is what actually gates traffic.
    try:
        await check_database_ready()
    except Exception as exc:
        # Broad on purpose: this is startup diagnostics, and any failure mode
        # here should be logged rather than allowed to abort the process.
        logger.critical("Database readiness check failed at startup: %s", exc)

    yield

    await dispose_engine()


app = FastAPI(
    title="llm-observe API",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    """Liveness. Deliberately does not touch the database."""
    return {"status": "ok", "environment": get_settings().environment}


@app.get("/readyz", tags=["ops"])
async def readyz() -> dict[str, str]:
    """Readiness: Postgres is reachable and row-level security is in force.

    Raising here is intentional. An instance whose runtime role bypasses RLS
    must never receive traffic, so a misconfigured DATABASE_URL fails the
    deploy loudly instead of silently serving cross-tenant data.
    """
    await check_database_ready()
    return {"status": "ready"}


app.include_router(ingest.router)
app.include_router(traces.router)
