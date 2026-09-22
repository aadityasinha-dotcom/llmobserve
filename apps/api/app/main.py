import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.db import check_database_ready, dispose_engine
from app.routers import auth, ingest, keys, traces

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


def _invalid_settings_fields(exc: ValidationError) -> list[str]:
    """Environment variable names that failed validation - names only.

    Never the values. A settings error can be raised by DATABASE_URL, and
    pydantic puts the offending input in `input_value`, so echoing the error
    itself would publish the database password in an HTTP response. The full
    error, values included, goes to the logs.
    """
    names = set()
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"])
        names.add(field.upper() if field else "?")
    return sorted(names)


def _settings_or_503() -> Settings:
    """Load settings, or fail with a response that says which variable is wrong.

    Configuration is supplied per environment, so a bad value is the single most
    likely reason a fresh deployment does not work. A bare 500 here sends the
    reader to the function logs to find out why; naming the variable saves that
    round trip - which on a serverless platform means another deploy cycle.
    """
    try:
        return get_settings()
    except ValidationError as exc:
        logger.critical("Invalid configuration, refusing to serve: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "config_error",
                "invalid_env_vars": _invalid_settings_fields(exc),
                "hint": "Fix these environment variables; values are in the logs, not here.",
            },
        ) from exc


@app.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    """Liveness. Deliberately does not touch the database.

    Still fails when configuration is invalid: an instance that cannot read its
    own settings cannot serve, and reporting "ok" would hide that.
    """
    return {"status": "ok", "environment": _settings_or_503().environment}


@app.get("/readyz", tags=["ops"])
async def readyz() -> dict[str, str]:
    """Readiness: Postgres is reachable and row-level security is in force.

    Raising here is intentional. An instance whose runtime role bypasses RLS
    must never receive traffic, so a misconfigured DATABASE_URL fails the
    deploy loudly instead of silently serving cross-tenant data.
    """
    _settings_or_503()
    await check_database_ready()
    return {"status": "ready"}


app.include_router(ingest.router)
app.include_router(traces.router)
app.include_router(auth.router)
app.include_router(keys.router)
