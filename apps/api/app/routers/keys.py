"""Self-serve API keys, for a signed-in user, on a project they belong to.

Every write here goes through a SECURITY DEFINER function that checks membership
in Postgres (migration 0007); the application role has no INSERT or UPDATE on
api_keys. The membership test in Python below is for a clean 404, not the
security boundary - the function would refuse regardless.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_scope
from app.deps import CurrentUser, SessionUser
from app.schemas.auth import ApiKeyCreate, ApiKeyCreated, ApiKeyOut
from app.services.api_keys import generate_api_key

router = APIRouter(prefix="/v1/projects/{project_id}/keys", tags=["api keys"])

DbSession = Annotated[AsyncSession, Depends(session_scope)]

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
_INSUFFICIENT_PRIVILEGE = "42501"


def _member_of(user: SessionUser, project_id: UUID) -> None:
    # 404, not 403: never confirm that a project the caller cannot see exists.
    if project_id not in user.project_ids:
        raise _NOT_FOUND


def _refused_by_database(exc: DBAPIError) -> bool:
    orig = exc.orig
    return (getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)) == (
        _INSUFFICIENT_PRIVILEGE
    )


@router.get("", response_model=list[ApiKeyOut], summary="List a project's API keys")
async def list_keys(project_id: UUID, user: CurrentUser, session: DbSession) -> list[ApiKeyOut]:
    _member_of(user, project_id)
    try:
        rows = (
            await session.execute(
                text("SELECT * FROM auth_list_api_keys(:u, :p)"),
                {"u": user.user_id, "p": project_id},
            )
        ).all()
    except DBAPIError as exc:
        if _refused_by_database(exc):
            raise _NOT_FOUND from exc
        raise
    return [ApiKeyOut.model_validate(row._mapping) for row in rows]


@router.post(
    "",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create an API key. The raw key is returned once.",
)
async def create_key(
    project_id: UUID, body: ApiKeyCreate, user: CurrentUser, session: DbSession
) -> ApiKeyCreated:
    _member_of(user, project_id)
    new_key = generate_api_key()
    scopes = sorted(set(body.scopes))
    # The session is shared with the CurrentUser dependency, which has already
    # read from it and so opened a transaction: commit that one rather than
    # beginning another. On failure, closing the session rolls it back.
    try:
        key_id = await session.scalar(
            text("SELECT auth_create_api_key(:u, :p, :h, :pre, :l, :s)"),
            {
                "u": user.user_id,
                "p": project_id,
                "h": new_key.key_hash,
                "pre": new_key.display_prefix,
                "l": body.label,
                "s": scopes,
            },
        )
        created = (
            await session.execute(
                text("SELECT created_at FROM api_keys WHERE id = :k"), {"k": key_id}
            )
        ).one()
        await session.commit()
    except DBAPIError as exc:
        if _refused_by_database(exc):
            raise _NOT_FOUND from exc
        raise
    return ApiKeyCreated(
        id=key_id,
        key_prefix=new_key.display_prefix,
        label=body.label,
        scopes=scopes,
        created_at=created.created_at,
        revoked_at=None,
        api_key=new_key.raw,
    )


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
    responses={404: {"description": "No such active key in a project you belong to"}},
)
async def revoke_key(project_id: UUID, key_id: UUID, user: CurrentUser, session: DbSession) -> None:
    _member_of(user, project_id)
    # Shared session with CurrentUser; see create_key.
    revoked = await session.scalar(
        text("SELECT auth_revoke_api_key(:u, :k)"), {"u": user.user_id, "k": key_id}
    )
    await session.commit()
    if not revoked:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
