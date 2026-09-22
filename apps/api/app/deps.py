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
from app.services.sessions import (
    InvalidSessionError,
    looks_like_session_token,
    verify_session_token,
)

logger = logging.getLogger(__name__)

# GUC read by the row-level security policies. Must match the migration.
TENANT_GUC = "app.current_project_id"

# auto_error=False so a missing header produces our own 401 with a
# WWW-Authenticate challenge rather than FastAPI's bare 403.
_bearer = HTTPBearer(
    auto_error=False,
    description="A project API key, or a dashboard session token from /v1/auth/google",
)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or missing API key",
    headers={"WWW-Authenticate": "Bearer"},
)

_SESSION_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Session is invalid or has expired. Sign in again.",
    headers={"WWW-Authenticate": "Bearer"},
)

# A project the caller may not see gets the same answer as one that does not
# exist, so the response never confirms that an id belongs to someone else.
_PROJECT_NOT_FOUND = HTTPException(
    status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
)

#: What a signed-in dashboard user may do with a project they belong to. Read
#: only: traces arrive through API keys, never through a browser session.
SESSION_SCOPES = frozenset({READ_SCOPE})


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
    """The authenticated caller: which project, and what it may do there.

    Exactly one of `key_id` and `user_id` is set, depending on whether the
    caller presented an API key or a dashboard session.
    """

    project_id: UUID
    scopes: frozenset[str]
    key_id: UUID | None = None
    user_id: UUID | None = None

    def can(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass(frozen=True)
class SessionUser:
    """A signed-in person, and every project they may select."""

    user_id: UUID
    email: str
    name: str | None
    avatar_url: str | None
    # (project_id, name, role), oldest membership first so the default project
    # is stable - it is the personal one created at sign-up.
    projects: tuple[tuple[UUID, str, str], ...]

    @property
    def project_ids(self) -> frozenset[UUID]:
        return frozenset(project_id for project_id, _, _ in self.projects)


def _bearer_token(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _UNAUTHORIZED
    token = credentials.credentials.strip()
    if not token:
        raise _UNAUTHORIZED
    return token


async def _load_session_user(token: str, session: AsyncSession) -> SessionUser:
    """Verify a session token and load the user it names.

    The signature proves the API issued it. The session_version comparison
    proves the user has not signed out since: stateless tokens cannot be deleted,
    so signing out moves the version on and every older token stops matching.
    """
    try:
        claims = verify_session_token(token)
    except InvalidSessionError:
        raise _SESSION_UNAUTHORIZED from None

    user = (
        await session.execute(
            text("SELECT id, email, name, avatar_url, session_version FROM users WHERE id = :u"),
            {"u": claims.user_id},
        )
    ).first()
    if user is None or user.session_version != claims.session_version:
        raise _SESSION_UNAUTHORIZED

    projects = (
        await session.execute(
            text(
                "SELECT p.id, p.name, m.role FROM project_members m "
                "JOIN projects p ON p.id = m.project_id "
                "WHERE m.user_id = :u ORDER BY m.created_at, p.id"
            ),
            {"u": claims.user_id},
        )
    ).all()
    return SessionUser(
        user_id=user.id,
        email=user.email,
        name=user.name,
        avatar_url=user.avatar_url,
        projects=tuple((row.id, row.name, row.role) for row in projects),
    )


async def get_session_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(session_scope)],
) -> SessionUser:
    """For routes that act on behalf of a person: /v1/me, key management.

    An API key here is a 403, not a 401. It is a valid credential of the wrong
    kind, and a key must never be able to mint further keys - otherwise leaking
    an ingest key would leak the ability to issue a read key.
    """
    token = _bearer_token(credentials)
    if not looks_like_session_token(token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint needs a signed-in dashboard user, not an API key.",
        )
    return await _load_session_user(token, session)


CurrentUser = Annotated[SessionUser, Depends(get_session_user)]


def resolve_project(user: SessionUser, requested: str | None) -> UUID:
    """Pick the project a session request is about.

    Explicit when the user has several (the dashboard sends X-Project-Id);
    implicit when they have exactly one, which is every new user.
    """
    if requested is None:
        if len(user.projects) == 1:
            return user.projects[0][0]
        if not user.projects:
            raise _PROJECT_NOT_FOUND
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You belong to several projects; send X-Project-Id to choose one.",
        )
    try:
        project_id = UUID(requested)
    except ValueError:
        raise _PROJECT_NOT_FOUND from None
    if project_id not in user.project_ids:
        raise _PROJECT_NOT_FOUND
    return project_id


async def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(session_scope)],
    x_project_id: Annotated[str | None, Header(alias="X-Project-Id")] = None,
) -> Principal:
    """Resolve the bearer credential to a project and what it may do there.

    Two kinds of credential share the header:

    * **API key** - resolves to its project and its own scopes. X-Project-Id is
      ignored: the key is authoritative about which project it belongs to.
    * **Session token** - resolves to a user, then to the project named by
      X-Project-Id, which must be one they are a member of. Sessions carry the
      read scope only.

    Unknown, revoked and expired credentials are all a 401, deliberately
    indistinguishable: telling someone a key is real but revoked confirms it was
    once valid. Revocation is a timestamp, never a delete, so a revoked hash
    cannot be reissued to a different project.
    """
    token = _bearer_token(credentials)

    if looks_like_session_token(token):
        user = await _load_session_user(token, session)
        return Principal(
            project_id=resolve_project(user, x_project_id),
            scopes=SESSION_SCOPES,
            user_id=user.user_id,
        )

    row = (
        await session.execute(
            select(ApiKey.id, ApiKey.project_id, ApiKey.scopes).where(
                ApiKey.key_hash == hash_api_key(token),
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
        if not principal.can(scope) and principal.user_id is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Dashboard sessions cannot use '{scope}'. "
                    "Create an API key with that scope in Settings → API keys."
                ),
            )
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
