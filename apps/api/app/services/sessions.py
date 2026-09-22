"""The API's own session tokens for signed-in dashboard users.

Google proves who someone is once, at sign-in. After that the dashboard presents
one of these on every request, as `Authorization: Bearer <token>` - the same
header an API key uses, told apart by shape (a JWT has three dot-separated
segments; an API key has none).

HS256 with a secret only the API holds. The dashboard stores the token in an
httpOnly cookie and forwards it; it cannot mint or alter one, because it never
has the key. That matters: a compromised dashboard can replay a session it is
handed, but it cannot become an arbitrary user.

Stateless tokens cannot be revoked individually, so each carries the user's
`session_version`, which the API compares against the database on every request.
Signing out increments it and every outstanding token for that user dies at once.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

from app.config import get_settings

ALGORITHM = "HS256"
ISSUER = "llm-observe-api"
AUDIENCE = "llm-observe-dashboard"
_TYPE = "session"


class InvalidSessionError(Exception):
    pass


@dataclass(frozen=True)
class SessionClaims:
    user_id: UUID
    session_version: int


def looks_like_session_token(bearer: str) -> bool:
    """A JWT has exactly three dot-separated segments; API keys have no dots.

    `secrets.token_urlsafe` never emits a dot, so the two credential kinds
    cannot be confused, and this sniff decides only which verifier runs - it
    grants nothing on its own.
    """
    return bearer.count(".") == 2


def issue_session_token(user_id: UUID, session_version: int) -> tuple[str, datetime]:
    settings = get_settings()
    if not settings.session_secret:
        raise InvalidSessionError("SESSION_SECRET is not configured")
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=settings.session_ttl_hours)
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": str(user_id),
            "typ": _TYPE,
            "sv": session_version,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        },
        settings.session_secret,
        algorithm=ALGORITHM,
    )
    return token, expires_at


def verify_session_token(token: str) -> SessionClaims:
    settings = get_settings()
    if not settings.session_secret:
        raise InvalidSessionError("SESSION_SECRET is not configured")
    try:
        claims = jwt.decode(
            token,
            settings.session_secret,
            # Pinned. Accepting the algorithm named in the token's own header is
            # how "alg: none" and RS/HS confusion attacks work.
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidSessionError(str(exc)) from exc

    if claims.get("typ") != _TYPE or not isinstance(claims.get("sv"), int):
        raise InvalidSessionError("not a session token")
    try:
        user_id = UUID(str(claims["sub"]))
    except ValueError as exc:
        raise InvalidSessionError("malformed subject") from exc
    return SessionClaims(user_id=user_id, session_version=claims["sv"])
