"""/healthz must say which environment variable is wrong, without leaking values.

A serverless deployment has no shell to poke at: if a bad env var produces only
"Internal Server Error", finding it costs a trip to the log viewer and often
another deploy. These tests pin both halves of the contract - the variable is
named, and its value never is.
"""

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app import config, main


def _raise_invalid(*_args: object, **_kwargs: object) -> config.Settings:
    """Stand in for the real loader, failing the way a bad env var does."""
    try:
        config.Settings(
            db_pool_mode="Transaction",  # type: ignore[arg-type]
            database_url="postgresql+asyncpg://u:sup3rsecret@host/db",
            db_require_rls="not-a-bool",  # type: ignore[arg-type]
        )
    except ValidationError:
        raise
    raise AssertionError("expected the settings to be rejected")


async def test_healthz_names_the_invalid_variables(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "get_settings", _raise_invalid)

    response = await client.get("/healthz")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["status"] == "config_error"
    assert detail["invalid_env_vars"] == ["DB_POOL_MODE", "DB_REQUIRE_RLS"]


async def test_the_response_never_carries_the_rejected_values(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settings error can come from DATABASE_URL, whose value is a credential."""
    monkeypatch.setattr(main, "get_settings", _raise_invalid)

    body = (await client.get("/healthz")).text

    assert "sup3rsecret" not in body
    assert "not-a-bool" not in body
    assert "Transaction" not in body


async def test_readyz_fails_on_bad_config_before_touching_the_database(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _must_not_run() -> None:
        raise AssertionError("readyz reached the database with invalid settings")

    monkeypatch.setattr(main, "get_settings", _raise_invalid)
    monkeypatch.setattr(main, "check_database_ready", _must_not_run)

    assert (await client.get("/readyz")).status_code == 503


async def test_healthz_is_ok_with_valid_settings(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
