"""Scoped API keys.

The point of the `api_keys` table is that a credential shipped inside someone
else's process can write traces without being able to read every stored prompt
back out. These tests hold that line from both directions, and cover the two
states a key can be in that are easy to get wrong: revoked, and legacy.
"""

from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient

from tests.conftest import Tenant, issue_key, seed_trace


def _auth(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


async def test_ingest_key_cannot_read_traces(
    client: AsyncClient, tenants: tuple[Tenant, Tenant], admin_engine: Any
) -> None:
    """The whole reason the table exists."""
    a, _ = tenants
    await seed_trace(a.project_id, "secret-prompt", with_observation=True)
    key = await issue_key(admin_engine, a.project_id, ["ingest"], "sdk")

    listed = await client.get("/v1/traces", headers=_auth(key))
    detail = await client.get(f"/v1/traces/{uuid4()}", headers=_auth(key))

    # 403, not 401: the key is valid, so retrying or re-prompting for it is not
    # the fix. And not 200-with-nothing, which would hide the misconfiguration.
    assert listed.status_code == 403
    assert detail.status_code == 403
    assert "read" in listed.json()["detail"]
    # The refusal must not leak what it is refusing.
    assert "secret-prompt" not in listed.text


async def test_read_key_cannot_write_traces(
    client: AsyncClient, tenants: tuple[Tenant, Tenant], admin_engine: Any
) -> None:
    """A dashboard key is a viewer, not a writer."""
    a, _ = tenants
    key = await issue_key(admin_engine, a.project_id, ["read"], "dashboard")

    response = await client.post("/v1/ingest", headers=_auth(key), json={"events": []})

    assert response.status_code == 403
    assert "ingest" in response.json()["detail"]


async def test_each_scope_works_on_its_own_route(
    client: AsyncClient, tenants: tuple[Tenant, Tenant], admin_engine: Any
) -> None:
    a, _ = tenants
    ingest_key = await issue_key(admin_engine, a.project_id, ["ingest"], "sdk")
    read_key = await issue_key(admin_engine, a.project_id, ["read"], "dashboard")

    trace_id = uuid4()
    written = await client.post(
        "/v1/ingest",
        headers=_auth(ingest_key),
        json={
            "events": [
                {"id": str(trace_id), "type": "trace", "name": "scoped"},
            ]
        },
    )
    assert written.status_code == 202

    listed = await client.get("/v1/traces", params={"name": "scoped"}, headers=_auth(read_key))
    assert listed.status_code == 200
    # Separate keys, same project: the two credentials must see one dataset.
    assert [t["id"] for t in listed.json()["data"]] == [str(trace_id)]


async def test_revoked_key_is_rejected(
    client: AsyncClient, tenants: tuple[Tenant, Tenant], admin_engine: Any
) -> None:
    """401 and not 403: a revoked key must be indistinguishable from an unknown
    one, or the response confirms that it was once real."""
    a, _ = tenants
    key = await issue_key(admin_engine, a.project_id, ["ingest", "read"], "old", revoked=True)

    assert (await client.get("/v1/traces", headers=_auth(key))).status_code == 401
    assert (
        await client.post("/v1/ingest", headers=_auth(key), json={"events": []})
    ).status_code == 401


async def test_revoking_one_key_leaves_the_others_working(
    client: AsyncClient, tenants: tuple[Tenant, Tenant], admin_engine: Any
) -> None:
    """The failure the old single-column design could not avoid."""
    a, _ = tenants
    live = await issue_key(admin_engine, a.project_id, ["read"], "dashboard")
    await issue_key(admin_engine, a.project_id, ["read"], "leaked", revoked=True)

    assert (await client.get("/v1/traces", headers=_auth(live))).status_code == 200


async def test_scopes_do_not_cross_projects(
    client: AsyncClient, tenants: tuple[Tenant, Tenant], admin_engine: Any
) -> None:
    """A read scope is permission to read *your* project, not any project."""
    a, b = tenants
    await seed_trace(a.project_id, "a-only", with_observation=True)
    b_key = await issue_key(admin_engine, b.project_id, ["read"], "dashboard")

    response = await client.get("/v1/traces", headers=_auth(b_key))

    assert response.status_code == 200
    assert response.json()["data"] == []


@pytest.mark.parametrize("scopes", [["ingest"], ["read"], ["ingest", "read"]])
async def test_every_scope_combination_still_authenticates(
    client: AsyncClient, tenants: tuple[Tenant, Tenant], admin_engine: Any, scopes: list[str]
) -> None:
    """Authentication and authorisation stay separate: a valid key is never 401."""
    a, _ = tenants
    key = await issue_key(admin_engine, a.project_id, scopes, "x")

    response = await client.get("/v1/traces", headers=_auth(key))

    assert response.status_code != 401
    assert response.status_code == (200 if "read" in scopes else 403)


async def test_unknown_scope_is_refused_by_the_database(
    tenants: tuple[Tenant, Tenant], admin_engine: Any
) -> None:
    """The check constraint is the backstop for a typo in a scope name.

    Without it, `--scopes reed` would mint a key that authenticates and is
    refused by every route, which looks like a server bug rather than a typo.
    """
    a, _ = tenants
    with pytest.raises(Exception, match="ck_api_keys_scopes_known"):
        await issue_key(admin_engine, a.project_id, ["reed"], "typo")


async def test_migrated_keys_keep_working(
    client: AsyncClient, tenants: tuple[Tenant, Tenant]
) -> None:
    """Migration 0006 backfills existing keys with both scopes.

    The fixture keys stand in for them. Narrowing on upgrade would have revoked
    a live SDK's access as a side effect of a deploy.
    """
    a, _ = tenants
    assert (await client.get("/v1/traces", headers=a.headers)).status_code == 200
    assert (
        await client.post("/v1/ingest", headers=a.headers, json={"events": []})
    ).status_code == 202
