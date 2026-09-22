"""Sign-in with Google, sign-out, and who am I.

The browser never talks to this router. The dashboard's server does: it runs the
redirect half of the OAuth flow on its own origin (so its cookie is first-party)
and calls here to complete it. See app.services.google_oauth for why the code
exchange lives in the API.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import session_scope
from app.deps import CurrentUser, SessionUser
from app.schemas.auth import GoogleSignInRequest, MeOut, ProjectOut, SessionOut, UserOut
from app.services.google_oauth import GoogleAuthError, exchange_code
from app.services.sessions import issue_session_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["auth"])

DbSession = Annotated[AsyncSession, Depends(session_scope)]

# One message for every way a sign-in can fail at Google's end. The specific
# reason (expired code, nonce mismatch, bad signature) goes to the log: it helps
# an operator and helps an attacker probing the endpoint equally.
_SIGN_IN_FAILED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Google sign-in could not be completed."
)


def _require_auth_configured() -> None:
    if not get_settings().auth_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Sign-in is not configured on this API. Set GOOGLE_CLIENT_ID, "
                "GOOGLE_CLIENT_SECRET and SESSION_SECRET."
            ),
        )


def _user_out(user: SessionUser) -> UserOut:
    return UserOut(id=user.user_id, email=user.email, name=user.name, avatar_url=user.avatar_url)


@router.post(
    "/auth/google",
    response_model=SessionOut,
    summary="Complete a Google sign-in and receive a session token",
    responses={
        400: {"description": "redirect_uri is not an allowed sign-in destination"},
        401: {"description": "Google rejected the code, or the ID token failed verification"},
        403: {"description": "Unverified email, or sign-up is restricted"},
        503: {"description": "Sign-in is not configured on this API"},
    },
)
async def sign_in_with_google(body: GoogleSignInRequest, session: DbSession) -> SessionOut:
    _require_auth_configured()
    settings = get_settings()

    if body.redirect_uri not in settings.allowed_redirect_uris:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="redirect_uri is not an allowed sign-in destination (AUTH_REDIRECT_URIS).",
        )

    try:
        identity = await exchange_code(body.code, body.code_verifier, body.redirect_uri, body.nonce)
    except GoogleAuthError as exc:
        logger.warning("Google sign-in failed: %s", exc)
        raise _SIGN_IN_FAILED from exc

    if not identity.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your Google account's email address is not verified.",
        )

    async with session.begin():
        existing = await session.scalar(
            text("SELECT id FROM users WHERE google_sub = :s"), {"s": identity.sub}
        )
        # The allowlist gates account creation only. Someone admitted once
        # keeps access even if the list later narrows; removing a person is
        # done by removing their memberships.
        if existing is None and not settings.may_sign_up(identity.email):
            logger.info("Sign-up refused for %s: not on the allowlist", identity.email)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Sign-ups are restricted on this deployment.",
            )

        row = (
            await session.execute(
                text("SELECT * FROM auth_sign_in_google(:sub, :email, :verified, :name, :avatar)"),
                {
                    "sub": identity.sub,
                    "email": identity.email,
                    "verified": identity.email_verified,
                    "name": identity.name,
                    "avatar": identity.picture,
                },
            )
        ).one()

    token, expires_at = issue_session_token(row.user_id, row.session_version)
    logger.info("Signed in user=%s created=%s", row.user_id, row.created)
    return SessionOut(
        session_token=token,
        expires_at=expires_at,
        user=UserOut(
            id=row.user_id, email=identity.email, name=identity.name, avatar_url=identity.picture
        ),
        created=row.created,
    )


@router.get("/me", response_model=MeOut, summary="The signed-in user and their projects")
async def me(user: CurrentUser) -> MeOut:
    return MeOut(
        user=_user_out(user),
        projects=[
            ProjectOut(id=project_id, name=name, role=role)
            for project_id, name, role in user.projects
        ],
    )


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out everywhere",
)
async def logout(user: CurrentUser, session: DbSession) -> None:
    """Invalidate every session this user holds, on every device.

    Stateless tokens cannot be deleted one at a time. Incrementing the user's
    session_version makes every token carrying the old one fail verification,
    so this is sign-out-everywhere by construction. Clearing the dashboard's
    cookie alone would leave a copied token valid until it expired.
    """
    # The session is shared with the CurrentUser dependency, which has already
    # read from it and so opened a transaction; commit that one rather than
    # beginning another.
    await session.execute(text("SELECT auth_bump_session_version(:u)"), {"u": user.user_id})
    await session.commit()
