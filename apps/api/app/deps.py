"""Request-scoped dependencies: authentication and tenant-bound sessions."""

import hashlib
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import check_database_ready, get_sessionmaker, session_scope
from app.models import INGEST_SCOPE, READ_SCOPE, ApiKey

logger = logging.getLogger(__name__)

# GUC read by the row-level security policies. Must match the migration.
TENANT_GUC = "app.current_project_id"

# auto_error=False so a missing header produces our own 401 with a
# WWW-Authenticate challenge rather than FastAPI's bare 403.
_bearer = HTTPBearer(auto_error=False, description="API key issued for a project")

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or missing API key",
    headers={"WWW-Authenticate": "Bearer"},
)


def hash_api_key(raw_key: str) -> str:
    """Hash a raw API key for storage and lookup.

    Plain SHA-256, unsalted, deliberately. API keys are long random strings, not
    user-chosen passwords, so they are not brute-forceable from a hash and need
    no key-stretching. Unsalted is also what makes the lookup a single indexed
    equality match instead of a scan-and-compare over every project.
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: which project, and what it may do."""

    project_id: UUID
    key_id: UUID
    scopes: frozenset[str]

    def can(self, scope: str) -> bool:
        return scope in self.scopes


async def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(session_scope)],
) -> Principal:
    """Resolve `Authorization: Bearer <api_key>` to a project and its scopes.

    Raises 401 for a missing header, a non-Bearer scheme, a key that matches no
    row, or a key that has been revoked. The failure modes are deliberately
    indistinguishable to the caller: telling someone that a key is real but
    revoked confirms that it was once valid.

    Revocation is checked here rather than by deleting the row, so a revoked
    hash can never be reissued to a different project.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _UNAUTHORIZED

    raw_key = credentials.credentials.strip()
    if not raw_key:
        raise _UNAUTHORIZED

    row = (
        await session.execute(
            select(ApiKey.id, ApiKey.project_id, ApiKey.scopes).where(
                ApiKey.key_hash == hash_api_key(raw_key),
                ApiKey.revoked_at.is_(None),
            )
        )
    ).first()
    if row is None:
        raise _UNAUTHORIZED

    return Principal(project_id=row.project_id, key_id=row.id, scopes=frozenset(row.scopes))


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


async def get_project_id(principal: CurrentPrincipal) -> UUID:
    """The authenticated project. Kept as its own dependency because the tenant
    session needs the project id without caring which scopes came with it."""
    return principal.project_id


CurrentProjectId = Annotated[UUID, Depends(get_project_id)]


def require_scope(scope: str) -> Callable[[Principal], Coroutine[Any, Any, Principal]]:
    """Build a dependency that admits only keys carrying `scope`.

    403, not 401: the caller authenticated successfully, so re-presenting the
    credential will not help, and a 401 would invite a client to retry or prompt
    for another key. The message names the scope required because that is the
    fix, and it discloses nothing the key holder does not already have.
    """

    async def _guard(principal: CurrentPrincipal) -> Principal:
        if not principal.can(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"This API key does not carry the '{scope}' scope. "
                    f'Issue one with: make key NAME=<project> ARGS="--add --scopes {scope}"'
                ),
            )
        return principal

    return _guard


#: Write path. An SDK key needs nothing more than this.
RequireIngest = Annotated[Principal, Depends(require_scope(INGEST_SCOPE))]
#: Read path. The dashboard needs only this, and must not be able to write.
RequireRead = Annotated[Principal, Depends(require_scope(READ_SCOPE))]


# Set once this process has confirmed its database role cannot bypass RLS.
# Deliberately not guarded by a lock: two cold requests racing here each run
# one read-only query, which is cheaper than a lock that would have to be bound
# to an event loop.
_rls_verified = False

_SERVICE_MISCONFIGURED = HTTPException(
    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    detail="Service is misconfigured and is refusing tenant data. See server logs.",
)


async def _ensure_rls_enforced() -> None:
    """Refuse tenant-scoped work until this process has proven RLS applies to it.

    /readyz does this job on a platform with readiness probes: an instance whose
    role bypasses RLS never receives traffic. Serverless platforms such as
    Vercel have no such gate - a deployment goes live and serves whatever it
    serves, and ASGI lifespan events are not a dependable place to stop it. So
    the same check runs here, once per process, in front of the first request
    that would touch tenant data.

    Fails closed with a generic 503. The detail stays in the logs: telling an
    unauthenticated-adjacent caller which role the service connects as is
    information it has no use for.
    """
    global _rls_verified
    if _rls_verified:
        return
    try:
        await check_database_ready()
    except RuntimeError as exc:
        logger.critical("Refusing tenant-scoped request: %s", exc)
        raise _SERVICE_MISCONFIGURED from exc
    _rls_verified = True


async def get_tenant_session(project_id: CurrentProjectId) -> AsyncIterator[AsyncSession]:
    """Yield a session whose transaction is bound to the authenticated project.

    Every statement issued on this session is filtered by the row-level security
    policies against the GUC set here. `set_config(..., is_local => true)` scopes
    the setting to the transaction, so a pooled connection cannot leak tenant
    context into the next request.

    Passing the id as a bind parameter rather than interpolating it into a
    `SET LOCAL` statement keeps this injection-proof.
    """
    await _ensure_rls_enforced()
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            text("SELECT set_config(:guc, :project_id, true)"),
            {"guc": TENANT_GUC, "project_id": str(project_id)},
        )
        yield session


TenantSession = Annotated[AsyncSession, Depends(get_tenant_session)]


async def get_sdk_version(
    x_sdk_version: Annotated[str | None, Header(alias="X-SDK-Version")] = None,
) -> str | None:
    """Client SDK version, recorded so deprecation decisions have data behind them."""
    return x_sdk_version


SdkVersion = Annotated[str | None, Depends(get_sdk_version)]
