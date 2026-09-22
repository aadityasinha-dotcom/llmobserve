"""Google sign-in, dashboard sessions, per-user isolation, and self-serve keys.

Google itself is replaced at exactly two seams - the token endpoint POST and the
JWKS key lookup - and nothing else. The ID tokens are real RS256 JWTs signed
with a key generated here, so signature, audience, issuer, expiry and nonce
verification all run for real. A test that stubbed out verification would
prove nothing about the part most worth proving.
"""

import time
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from sqlalchemy import text

from app.config import get_settings
from app.services import google_oauth
from tests.conftest import Tenant

CLIENT_ID = "test-client.apps.googleusercontent.com"
REDIRECT_URI = "http://localhost:3000/auth/google/callback"
NONCE = "n" * 32
VERIFIER = "v" * 64

_GOOGLE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_ATTACKER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def google_id_token(sub: str, email: str, **overrides: Any) -> str:
    """An ID token shaped exactly like Google's, signed with the test key."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": sub,
        "email": email,
        "email_verified": True,
        "name": "Test User",
        "iat": now,
        "exp": now + 3600,
        "nonce": NONCE,
    }
    signing_key = overrides.pop("signing_key", _GOOGLE_KEY)
    claims.update(overrides)
    return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "test"})


@pytest.fixture
def auth_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "google_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "google_client_secret", "test-secret")
    monkeypatch.setattr(settings, "session_secret", "s" * 64)
    monkeypatch.setattr(settings, "auth_redirect_uris", REDIRECT_URI)
    monkeypatch.setattr(settings, "signup_allowed_emails", "")
    monkeypatch.setattr(settings, "signup_allowed_domains", "")
    # Google's published key, as the JWKS lookup would return it.
    monkeypatch.setattr(google_oauth, "_signing_key", lambda _token: _GOOGLE_KEY.public_key())


@pytest.fixture
async def google(
    auth_settings: None, admin_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Callable[[str], None]]:
    """Queue the ID token Google's token endpoint will hand back next.

    Tracks every Google subject used, and deletes those users and their
    personal projects afterwards.
    """
    pending: list[str] = []
    subjects: list[str] = []

    async def fake_token_endpoint(form: dict[str, str]) -> dict[str, Any]:
        # The API must present the secret and the PKCE verifier; assert it does.
        assert form["client_secret"] == "test-secret"
        assert form["code_verifier"] == VERIFIER
        assert form["redirect_uri"] == REDIRECT_URI
        return {"id_token": pending.pop(0)}

    monkeypatch.setattr(google_oauth, "_post_token_request", fake_token_endpoint)

    def queue(token: str) -> None:
        pending.append(token)
        subjects.append(jwt.decode(token, options={"verify_signature": False})["sub"])

    yield queue

    async with admin_engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM projects WHERE created_by IN "
                "(SELECT id FROM users WHERE google_sub = ANY(:s))"
            ),
            {"s": subjects},
        )
        await conn.execute(text("DELETE FROM users WHERE google_sub = ANY(:s)"), {"s": subjects})


async def sign_in(client: AsyncClient, google: Callable[[str], None], token: str) -> Any:
    google(token)
    return await client.post(
        "/v1/auth/google",
        json={
            "code": "auth-code",
            "code_verifier": VERIFIER,
            "redirect_uri": REDIRECT_URI,
            "nonce": NONCE,
        },
    )


async def new_user(client: AsyncClient, google: Callable[[str], None]) -> dict[str, Any]:
    sub = f"sub-{uuid4().hex}"
    response = await sign_in(client, google, google_id_token(sub, f"{sub}@example.com"))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    body["headers"] = {"Authorization": f"Bearer {body['session_token']}"}
    return body


# ---------------------------------------------------------------------------
# Sign-up and sign-in
# ---------------------------------------------------------------------------


async def test_first_sign_in_creates_user_and_personal_project(
    client: AsyncClient, google: Callable[[str], None]
) -> None:
    user = await new_user(client, google)
    assert user["created"] is True

    me = (await client.get("/v1/me", headers=user["headers"])).json()
    assert me["user"]["id"] == user["user"]["id"]
    assert len(me["projects"]) == 1
    assert me["projects"][0]["role"] == "owner"


async def test_second_sign_in_reuses_the_account(
    client: AsyncClient, google: Callable[[str], None]
) -> None:
    sub = f"sub-{uuid4().hex}"
    first = (await sign_in(client, google, google_id_token(sub, "a@example.com"))).json()
    second = (await sign_in(client, google, google_id_token(sub, "a@example.com"))).json()

    assert second["created"] is False
    assert second["user"]["id"] == first["user"]["id"]
    me = (
        await client.get("/v1/me", headers={"Authorization": f"Bearer {second['session_token']}"})
    ).json()
    assert len(me["projects"]) == 1, "a second sign-in must not create a second project"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"aud": "someone-elses-client"}, id="wrong-audience"),
        pytest.param({"iss": "https://evil.example"}, id="wrong-issuer"),
        pytest.param({"nonce": "x" * 32}, id="replayed-nonce"),
        pytest.param({"exp": int(time.time()) - 3600}, id="expired"),
        pytest.param({"signing_key": _ATTACKER_KEY}, id="forged-signature"),
    ],
)
async def test_id_token_that_fails_verification_is_refused(
    client: AsyncClient, google: Callable[[str], None], overrides: dict[str, Any]
) -> None:
    token = google_id_token(f"sub-{uuid4().hex}", "a@example.com", **overrides)
    response = await sign_in(client, google, token)
    assert response.status_code == 401
    # The reason is logged, never returned.
    assert response.json()["detail"] == "Google sign-in could not be completed."


async def test_unverified_email_is_refused(
    client: AsyncClient, google: Callable[[str], None]
) -> None:
    token = google_id_token(f"sub-{uuid4().hex}", "a@example.com", email_verified=False)
    assert (await sign_in(client, google, token)).status_code == 403


async def test_unlisted_redirect_uri_is_refused(client: AsyncClient, auth_settings: None) -> None:
    response = await client.post(
        "/v1/auth/google",
        json={
            "code": "c",
            "code_verifier": VERIFIER,
            "redirect_uri": "https://evil.example/callback",
            "nonce": NONCE,
        },
    )
    assert response.status_code == 400


async def test_signup_allowlist_gates_new_accounts_only(
    client: AsyncClient, google: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = f"sub-{uuid4().hex}"
    # Admitted while sign-up is open.
    assert (await sign_in(client, google, google_id_token(sub, "a@old.example"))).status_code == 200

    monkeypatch.setattr(get_settings(), "signup_allowed_domains", "company.example")

    outsider = google_id_token(f"sub-{uuid4().hex}", "b@else.example")
    assert (await sign_in(client, google, outsider)).status_code == 403
    insider = google_id_token(f"sub-{uuid4().hex}", "c@company.example")
    assert (await sign_in(client, google, insider)).status_code == 200
    # Existing accounts keep working when the list narrows.
    assert (await sign_in(client, google, google_id_token(sub, "a@old.example"))).status_code == 200


async def test_sign_in_is_503_until_configured(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/auth/google",
        json={"code": "c", "code_verifier": VERIFIER, "redirect_uri": REDIRECT_URI, "nonce": NONCE},
    )
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------


def _forge(secret: str, **claims: Any) -> str:
    now = int(time.time())
    payload = {
        "iss": "llm-observe-api",
        "aud": "llm-observe-dashboard",
        "typ": "session",
        "sv": 1,
        "iat": now,
        "exp": now + 600,
    }
    payload.update(claims)
    return jwt.encode(payload, secret, algorithm="HS256")


async def test_forged_and_tampered_sessions_are_refused(
    client: AsyncClient, google: Callable[[str], None]
) -> None:
    user = await new_user(client, google)
    uid = user["user"]["id"]

    wrong_secret = _forge("not-the-api-secret" * 4, sub=uid)
    expired = _forge("s" * 64, sub=uid, exp=int(time.time()) - 10)
    header, payload, signature = user["session_token"].split(".")
    tampered = f"{header}.{payload}x.{signature}"
    unsigned = jwt.encode({"sub": uid}, key=None, algorithm="none")  # type: ignore[arg-type]

    for token in (wrong_secret, expired, tampered, unsigned):
        response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code in (401, 403), token


async def test_logout_invalidates_every_session(
    client: AsyncClient, google: Callable[[str], None]
) -> None:
    sub = f"sub-{uuid4().hex}"
    laptop = (await sign_in(client, google, google_id_token(sub, "a@example.com"))).json()
    phone = (await sign_in(client, google, google_id_token(sub, "a@example.com"))).json()
    laptop_h = {"Authorization": f"Bearer {laptop['session_token']}"}
    phone_h = {"Authorization": f"Bearer {phone['session_token']}"}

    assert (await client.post("/v1/auth/logout", headers=laptop_h)).status_code == 204

    assert (await client.get("/v1/me", headers=laptop_h)).status_code == 401
    assert (await client.get("/v1/me", headers=phone_h)).status_code == 401


# ---------------------------------------------------------------------------
# Isolation: user A sees only user A's traces
# ---------------------------------------------------------------------------


async def test_user_cannot_read_another_users_project(
    client: AsyncClient, google: Callable[[str], None]
) -> None:
    alice = await new_user(client, google)
    bob = await new_user(client, google)
    alice_project = (await client.get("/v1/me", headers=alice["headers"])).json()["projects"][0]

    # Alice sends a trace with a key she issued herself.
    key = (
        await client.post(
            f"/v1/projects/{alice_project['id']}/keys",
            headers=alice["headers"],
            json={"scopes": ["ingest"], "label": "sdk"},
        )
    ).json()["api_key"]
    trace_id = str(uuid4())
    written = await client.post(
        "/v1/ingest",
        headers={"Authorization": f"Bearer {key}"},
        json={"events": [{"id": trace_id, "type": "trace", "name": "alice-private"}]},
    )
    assert written.status_code == 202

    # Alice sees it.
    mine = await client.get("/v1/traces", headers=alice["headers"])
    assert [t["id"] for t in mine.json()["data"]] == [trace_id]

    # Bob sees nothing in his own project...
    assert (await client.get("/v1/traces", headers=bob["headers"])).json()["data"] == []
    # ...cannot select Alice's project...
    steal = {**bob["headers"], "X-Project-Id": alice_project["id"]}
    assert (await client.get("/v1/traces", headers=steal)).status_code == 404
    # ...cannot fetch the trace by id...
    assert (await client.get(f"/v1/traces/{trace_id}", headers=bob["headers"])).status_code == 404
    # ...and cannot list or mint keys for her project.
    assert (
        await client.get(f"/v1/projects/{alice_project['id']}/keys", headers=bob["headers"])
    ).status_code == 404
    assert (
        await client.post(
            f"/v1/projects/{alice_project['id']}/keys",
            headers=bob["headers"],
            json={"scopes": ["read"]},
        )
    ).status_code == 404


async def test_session_cannot_write_traces(
    client: AsyncClient, google: Callable[[str], None]
) -> None:
    user = await new_user(client, google)
    response = await client.post("/v1/ingest", headers=user["headers"], json={"events": []})
    assert response.status_code == 403
    assert "Dashboard sessions" in response.json()["detail"]


async def test_several_projects_require_an_explicit_choice(
    client: AsyncClient,
    google: Callable[[str], None],
    tenants: tuple[Tenant, Tenant],
    admin_engine: Any,
) -> None:
    user = await new_user(client, google)
    shared, _ = tenants
    async with admin_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO project_members (project_id, user_id) VALUES (:p, :u)"),
            {"p": shared.project_id, "u": UUID(user["user"]["id"])},
        )

    assert (await client.get("/v1/traces", headers=user["headers"])).status_code == 400
    chosen = {**user["headers"], "X-Project-Id": str(shared.project_id)}
    assert (await client.get("/v1/traces", headers=chosen)).status_code == 200


# ---------------------------------------------------------------------------
# Self-serve API keys
# ---------------------------------------------------------------------------


async def test_key_lifecycle(client: AsyncClient, google: Callable[[str], None]) -> None:
    user = await new_user(client, google)
    project_id = (await client.get("/v1/me", headers=user["headers"])).json()["projects"][0]["id"]
    base = f"/v1/projects/{project_id}/keys"

    created = await client.post(
        base, headers=user["headers"], json={"scopes": ["ingest"], "label": "sdk"}
    )
    assert created.status_code == 201
    raw = created.json()["api_key"]
    assert raw.startswith("llmo_sk_")

    listed = (await client.get(base, headers=user["headers"])).json()
    assert [k["label"] for k in listed] == ["sdk"]
    # The listing never carries the key or its hash.
    assert "api_key" not in listed[0] and "key_hash" not in listed[0]

    sdk = {"Authorization": f"Bearer {raw}"}
    assert (await client.post("/v1/ingest", headers=sdk, json={"events": []})).status_code == 202

    key_id = created.json()["id"]
    assert (await client.delete(f"{base}/{key_id}", headers=user["headers"])).status_code == 204
    assert (await client.post("/v1/ingest", headers=sdk, json={"events": []})).status_code == 401
    # Revoking twice is a 404, not a silent success.
    assert (await client.delete(f"{base}/{key_id}", headers=user["headers"])).status_code == 404


async def test_api_keys_cannot_act_as_a_user(
    client: AsyncClient, tenants: tuple[Tenant, Tenant]
) -> None:
    """A leaked ingest key must not be a way to mint a read key."""
    a, _ = tenants
    assert (await client.get("/v1/me", headers=a.headers)).status_code == 403
    response = await client.post(
        f"/v1/projects/{a.project_id}/keys", headers=a.headers, json={"scopes": ["read"]}
    )
    assert response.status_code == 403


async def test_unknown_scope_is_rejected_at_the_edge(
    client: AsyncClient, google: Callable[[str], None]
) -> None:
    user = await new_user(client, google)
    project_id = (await client.get("/v1/me", headers=user["headers"])).json()["projects"][0]["id"]
    response = await client.post(
        f"/v1/projects/{project_id}/keys", headers=user["headers"], json={"scopes": ["admin"]}
    )
    assert response.status_code == 422
