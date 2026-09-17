"""The per-process RLS guard in front of tenant-scoped requests.

On a platform with readiness probes, /readyz keeps an instance whose database
role bypasses RLS away from traffic. Serverless platforms have no such gate, so
app.deps checks once per process instead. These tests pin the behaviour that
matters: a misconfigured role gets a 503 for tenant data, not a working-looking
response with every policy silently switched off.
"""

import pytest
from httpx import AsyncClient

from app import deps
from tests.conftest import Tenant


async def _bypassing() -> None:
    raise RuntimeError("Runtime database role 'postgres' can bypass row-level security.")


async def test_bypassing_role_is_refused_for_reads_and_writes(
    client: AsyncClient, tenants: tuple[Tenant, Tenant], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = tenants
    monkeypatch.setattr(deps, "_rls_verified", False)
    monkeypatch.setattr(deps, "check_database_ready", _bypassing)

    read = await client.get("/v1/traces", headers=a.headers)
    write = await client.post("/v1/ingest", headers=a.headers, json={"events": []})

    assert read.status_code == 503
    assert write.status_code == 503
    # The role name stays in the logs, not the response.
    assert "postgres" not in read.text


async def test_refusal_is_not_cached_as_success(
    client: AsyncClient, tenants: tuple[Tenant, Tenant], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed check must be re-run, or one refusal would be followed by service."""
    a, _ = tenants
    monkeypatch.setattr(deps, "_rls_verified", False)
    monkeypatch.setattr(deps, "check_database_ready", _bypassing)

    for _ in range(2):
        response = await client.get("/v1/traces", headers=a.headers)
        assert response.status_code == 503
    assert deps._rls_verified is False


async def test_check_runs_once_per_process(
    client: AsyncClient, tenants: tuple[Tenant, Tenant], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = tenants
    calls = 0

    async def _counting() -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(deps, "_rls_verified", False)
    monkeypatch.setattr(deps, "check_database_ready", _counting)

    for _ in range(3):
        assert (await client.get("/v1/traces", headers=a.headers)).status_code == 200
    assert calls == 1


async def test_unauthenticated_requests_do_not_reach_the_check(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth runs first, so a bad key is still a 401 and costs no extra query."""
    monkeypatch.setattr(deps, "_rls_verified", False)
    monkeypatch.setattr(deps, "check_database_ready", _bypassing)

    response = await client.get("/v1/traces", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
