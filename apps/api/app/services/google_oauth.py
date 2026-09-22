"""Google sign-in: the server-side half of the OAuth authorisation-code flow.

The dashboard runs the browser half - it redirects to Google with `state`,
`nonce` and a PKCE challenge, and checks `state` when Google redirects back. It
then hands the authorisation code to the API, and everything that needs a secret
or decides who someone is happens here:

1. Exchange the code with Google's token endpoint, presenting the client secret
   and the PKCE verifier. The secret never leaves the API.
2. Verify the ID token Google returns: signature against Google's published
   keys, audience = our client id, issuer = Google, expiry, and the nonce the
   dashboard generated for this sign-in.

Nothing from the ID token is trusted until step 2 passes. Receiving the token
over TLS straight from Google's token endpoint is already strong evidence, but
verifying the signature costs one cached key lookup and removes the need to
reason about how the token arrived.
"""

import asyncio
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx
import jwt

from app.config import get_settings

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
# Google documents both spellings as valid issuers.
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

_EXCHANGE_TIMEOUT = httpx.Timeout(10.0)
# Tolerance for clock skew between Google and this function's host.
_LEEWAY_SECONDS = 60


class GoogleAuthError(Exception):
    """The sign-in could not be completed. The message is safe to log, not to
    return: it can describe why a token was rejected."""


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    email_verified: bool
    name: str | None
    picture: str | None


@lru_cache
def _jwks_client() -> jwt.PyJWKClient:
    # Caches Google's signing keys in-process. Google rotates them roughly
    # daily and publishes the next one in advance, so a cached set stays valid
    # across a rotation; an unknown `kid` triggers a refetch.
    return jwt.PyJWKClient(GOOGLE_JWKS_URL, cache_keys=True, lifespan=3600)


def _signing_key(id_token: str) -> Any:
    """The public key that must have signed this token. A seam for tests."""
    return _jwks_client().get_signing_key_from_jwt(id_token).key


async def _post_token_request(form: dict[str, str]) -> dict[str, Any]:
    """POST to Google's token endpoint. A seam for tests."""
    async with httpx.AsyncClient(timeout=_EXCHANGE_TIMEOUT) as client:
        response = await client.post(GOOGLE_TOKEN_URL, data=form)
    if response.status_code != 200:
        # Google's error body names the reason (invalid_grant, redirect_uri
        # mismatch). Keep it for the log; it never reaches the caller.
        raise GoogleAuthError(f"token endpoint returned {response.status_code}: {response.text}")
    body: dict[str, Any] = response.json()
    return body


def verify_id_token(id_token: str, expected_nonce: str) -> GoogleIdentity:
    settings = get_settings()
    try:
        claims = jwt.decode(
            id_token,
            _signing_key(id_token),
            algorithms=["RS256"],
            audience=settings.google_client_id,
            issuer=GOOGLE_ISSUERS,
            leeway=_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise GoogleAuthError(f"ID token rejected: {exc}") from exc

    # The nonce binds this token to the sign-in the dashboard started. Without
    # the check, a token obtained for one sign-in could be replayed into another.
    if not expected_nonce or claims.get("nonce") != expected_nonce:
        raise GoogleAuthError("ID token nonce does not match this sign-in")

    email = claims.get("email")
    if not isinstance(email, str) or not email:
        raise GoogleAuthError("ID token carries no email; request the 'email' scope")

    return GoogleIdentity(
        sub=str(claims["sub"]),
        email=email,
        # Google sends a real boolean; older tokens sent the string "true".
        email_verified=claims.get("email_verified") in (True, "true"),
        name=claims.get("name"),
        picture=claims.get("picture"),
    )


async def exchange_code(
    code: str, code_verifier: str, redirect_uri: str, nonce: str
) -> GoogleIdentity:
    """Trade an authorisation code for a verified Google identity."""
    settings = get_settings()
    if not (settings.google_client_id and settings.google_client_secret):
        raise GoogleAuthError("Google sign-in is not configured")

    tokens = await _post_token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
        }
    )
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str):
        raise GoogleAuthError("token response carried no id_token; request the 'openid' scope")

    # PyJWKClient is synchronous (urllib). Off the event loop, so one sign-in
    # fetching keys cannot stall every other request on this instance.
    return await asyncio.to_thread(verify_id_token, id_token, nonce)
