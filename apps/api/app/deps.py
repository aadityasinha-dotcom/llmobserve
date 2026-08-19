"""Request-scoped dependencies: authentication and tenant-bound sessions."""

import hashlib
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker, session_scope
from app.models import Project

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


async def get_project_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(session_scope)],
) -> UUID:
    """Resolve `Authorization: Bearer <api_key>` to a project id.

    Raises 401 for a missing header, a non-Bearer scheme, or a key that matches
    no project. The failure modes are deliberately indistinguishable to the
    caller.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _UNAUTHORIZED

    raw_key = credentials.credentials.strip()
    if not raw_key:
        raise _UNAUTHORIZED

    project_id = await session.scalar(
        select(Project.id).where(Project.api_key_hash == hash_api_key(raw_key))
    )
    if project_id is None:
        raise _UNAUTHORIZED

    return project_id


CurrentProjectId = Annotated[UUID, Depends(get_project_id)]


async def get_tenant_session(project_id: CurrentProjectId) -> AsyncIterator[AsyncSession]:
    """Yield a session whose transaction is bound to the authenticated project.

    Every statement issued on this session is filtered by the row-level security
    policies against the GUC set here. `set_config(..., is_local => true)` scopes
    the setting to the transaction, so a pooled connection cannot leak tenant
    context into the next request.

    Passing the id as a bind parameter rather than interpolating it into a
    `SET LOCAL` statement keeps this injection-proof.
    """
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
