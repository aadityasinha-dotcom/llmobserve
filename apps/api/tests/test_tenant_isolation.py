"""Project A cannot read project B's traces - step 7 of the build order.

CLAUDE.md: "Every query is project-scoped. API key resolves to a project_id.
Every table carries project_id and every read filters on it. Enforce with
Postgres row-level security, not by trusting WHERE clauses."

That last clause is what these tests are shaped around. It would be easy to
write a suite that passes because every query happens to carry the right WHERE
predicate - and which would therefore keep passing after someone forgets one.
So the queries below are deliberately hostile: they ask for the other tenant's
rows explicitly, by id, with no filter, and expect Postgres to refuse anyway.

The first group is the precondition. If the runtime role can bypass RLS, every
later assertion in this file would pass for the wrong reason - the rows would
be filtered by the WHERE clauses in the test itself rather than by any policy.
"""

from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import DBAPIError

from app.db import check_database_ready, get_engine, rls_is_bypassed
from tests.conftest import Tenant, seed_trace, tenant_session

# --------------------------------------------------------------------------
# Preconditions: isolation is capable of being enforced at all
# --------------------------------------------------------------------------


async def test_runtime_role_cannot_bypass_rls() -> None:
    """The role the app connects as must be subject to the policies.

    Superusers and BYPASSRLS roles ignore row-level security unconditionally, so
    if this fails nothing else in this file means anything - the data would be
    isolated only by the application's own WHERE clauses, which is exactly the
    arrangement CLAUDE.md rules out.
    """
    async with get_engine().connect() as conn:
        assert await rls_is_bypassed(conn) is False, (
            "DATABASE_URL points at a role that bypasses RLS. Tenant isolation "
            "is not being enforced."
        )


async def test_readiness_check_accepts_this_configuration() -> None:
    """The guard that fails a deploy in the state above."""
    await check_database_ready()


async def test_rls_is_enabled_and_forced_on_tenant_tables() -> None:
    """ENABLE alone is not enough: the table owner is exempt without FORCE."""
    async with get_engine().connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname IN ('traces', 'observations') "
                    "ORDER BY relname"
                )
            )
        ).all()

    assert [tuple(r) for r in rows] == [
        ("observations", True, True),
        ("traces", True, True),
    ]


async def test_projects_table_is_not_tenant_scoped() -> None:
    """`projects` is deliberately outside RLS, and read-only for the app role.

    Authentication has to resolve an API key to a project *before* a tenant is
    known, so a tenant predicate on this table would make login impossible. The
    compensating control is that the application role cannot write it.
    """
    async with get_engine().connect() as conn:
        forced = await conn.scalar(
            text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'projects'")
        )
        assert forced is False

        for privilege in ("INSERT", "UPDATE", "DELETE"):
            granted = await conn.scalar(
                text("SELECT has_table_privilege(current_user, 'projects', :p)"),
                {"p": privilege},
            )
            assert granted is False, f"app role should not hold {privilege} on projects"


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


async def test_tenant_sees_only_its_own_traces(tenants: tuple[Tenant, Tenant]) -> None:
    a, b = tenants
    await seed_trace(a.project_id, "a-one")
    await seed_trace(a.project_id, "a-two")
    await seed_trace(b.project_id, "b-one")

    async with tenant_session(a.project_id) as session:
        names = list(await session.scalars(text("SELECT name FROM traces ORDER BY name")))
    assert names == ["a-one", "a-two"]

    async with tenant_session(b.project_id) as session:
        names = list(await session.scalars(text("SELECT name FROM traces ORDER BY name")))
    assert names == ["b-one"]


async def test_tenant_sees_only_its_own_observations(tenants: tuple[Tenant, Tenant]) -> None:
    a, b = tenants
    await seed_trace(a.project_id, "a-obs", with_observation=True)
    await seed_trace(b.project_id, "b-obs", with_observation=True)

    for tenant in (a, b):
        async with tenant_session(tenant.project_id) as session:
            owners = set(
                await session.scalars(text("SELECT DISTINCT project_id FROM observations"))
            )
        assert owners == {tenant.project_id}


async def test_asking_for_the_other_tenant_by_id_returns_nothing(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """The hostile case: a query that explicitly targets B, issued as A.

    This is the one that a WHERE-clause-based implementation would fail. Nothing
    in the application is filtering here - the predicate asks *for* B's rows.
    """
    a, b = tenants
    b_trace = await seed_trace(b.project_id, "b-secret")

    async with tenant_session(a.project_id) as session:
        by_project = await session.scalar(
            text("SELECT count(*) FROM traces WHERE project_id = :pid"),
            {"pid": b.project_id},
        )
        by_id = await session.scalar(
            text("SELECT count(*) FROM traces WHERE id = :tid"), {"tid": b_trace}
        )
        unfiltered = await session.scalar(text("SELECT count(*) FROM traces"))

    assert by_project == 0
    assert by_id == 0
    assert unfiltered == 0


async def test_no_tenant_context_sees_nothing(tenants: tuple[Tenant, Tenant]) -> None:
    """A session that never set the GUC must see nothing, not everything.

    `current_setting(..., true)` returns NULL when unset, so the policy predicate
    is NULL rather than true. Worth pinning: the failure mode of getting this
    backwards is total exposure, and it is invisible in single-tenant testing.
    """
    a, _ = tenants
    await seed_trace(a.project_id, "a-only")

    async with get_engine().connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM traces")) == 0
        assert await conn.scalar(text("SELECT count(*) FROM observations")) == 0


async def test_tenant_context_does_not_leak_between_transactions(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """set_config(..., is_local => true) must not outlive its transaction.

    Under transaction-mode pooling the next request may land on this very
    connection. A session-level SET here would hand it the previous tenant's
    context - the precise reason the dependency scopes it to the transaction.
    """
    a, _ = tenants
    await seed_trace(a.project_id, "a-only")

    async with tenant_session(a.project_id) as session:
        assert await session.scalar(text("SELECT count(*) FROM traces")) == 1

    async with get_engine().connect() as conn:
        leaked = await conn.scalar(
            text("SELECT nullif(current_setting('app.current_project_id', true), '')")
        )
        assert leaked is None
        assert await conn.scalar(text("SELECT count(*) FROM traces")) == 0


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


async def test_cannot_insert_a_row_owned_by_another_tenant(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """WITH CHECK, not just USING: reads and writes are both constrained."""
    a, b = tenants

    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(a.project_id) as session:
            await session.execute(
                text(
                    "INSERT INTO traces (id, project_id, name, started_at) "
                    "VALUES (:id, :pid, 'smuggled', now())"
                ),
                {"id": uuid4(), "pid": b.project_id},
            )
    assert "row-level security" in str(exc.value).lower()

    async with tenant_session(b.project_id) as session:
        assert await session.scalar(text("SELECT count(*) FROM traces")) == 0


async def test_cannot_reassign_own_row_to_another_tenant(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """Owning a row is not permission to hand it to someone else."""
    a, b = tenants
    trace_id = await seed_trace(a.project_id, "a-owned")

    with pytest.raises(DBAPIError) as exc:
        async with tenant_session(a.project_id) as session:
            await session.execute(
                text("UPDATE traces SET project_id = :pid WHERE id = :tid"),
                {"pid": b.project_id, "tid": trace_id},
            )
    assert "row-level security" in str(exc.value).lower()

    async with tenant_session(a.project_id) as session:
        assert (
            await session.scalar(text("SELECT count(*) FROM traces WHERE id = :t"), {"t": trace_id})
            == 1
        )


async def test_update_cannot_touch_another_tenants_rows(
    tenants: tuple[Tenant, Tenant],
) -> None:
    """An unfiltered UPDATE issued as A must not modify B's rows.

    It does not error - the policy makes B's rows invisible, so the statement
    matches nothing. Silently affecting zero rows is the correct outcome.
    """
    a, b = tenants
    b_trace = await seed_trace(b.project_id, "b-original")

    async with tenant_session(a.project_id) as session:
        result = cast(
            CursorResult[Any], await session.execute(text("UPDATE traces SET name = 'overwritten'"))
        )
        assert result.rowcount == 0

    async with tenant_session(b.project_id) as session:
        name = await session.scalar(text("SELECT name FROM traces WHERE id = :t"), {"t": b_trace})
    assert name == "b-original"


async def test_application_role_cannot_delete(tenants: tuple[Tenant, Tenant]) -> None:
    """DELETE was never granted - a second, independent barrier to the same data.

    Ingest only ever inserts and upserts, so withholding DELETE costs nothing and
    means a policy mistake alone cannot destroy another tenant's history.
    """
    a, _ = tenants
    await seed_trace(a.project_id, "a-keep")

    async with get_engine().connect() as conn:
        assert (
            await conn.scalar(text("SELECT has_table_privilege(current_user, 'traces', 'DELETE')"))
            is False
        )


# --------------------------------------------------------------------------
# Through the HTTP surface
# --------------------------------------------------------------------------


async def test_ingested_rows_are_attributed_to_the_authenticated_project(
    tenants: tuple[Tenant, Tenant], client: object
) -> None:
    a, b = tenants
    trace_id = uuid4()

    response = await client.post(  # type: ignore[attr-defined]
        "/v1/ingest",
        json={"traces": [{"id": str(trace_id), "name": "via-api"}]},
        headers=a.headers,
    )
    assert response.status_code == 202

    async with tenant_session(a.project_id) as session:
        assert await session.scalar(text("SELECT count(*) FROM traces")) == 1
    async with tenant_session(b.project_id) as session:
        assert await session.scalar(text("SELECT count(*) FROM traces")) == 0


async def test_project_id_in_the_payload_is_ignored(
    tenants: tuple[Tenant, Tenant], client: object
) -> None:
    """A client cannot nominate the project it writes to.

    The ingest schemas have no project_id field, so `extra="ignore"` drops it
    before the router ever sees it. Sending one must be a no-op rather than an
    error - rule 5 forbids 4xxing on unrecognised fields - and it must certainly
    not redirect the write.
    """
    a, b = tenants

    response = await client.post(  # type: ignore[attr-defined]
        "/v1/ingest",
        json={
            "traces": [
                {
                    "id": str(uuid4()),
                    "name": "hijack-attempt",
                    "project_id": str(b.project_id),
                }
            ]
        },
        headers=a.headers,
    )
    assert response.status_code == 202

    async with tenant_session(b.project_id) as session:
        assert await session.scalar(text("SELECT count(*) FROM traces")) == 0
    async with tenant_session(a.project_id) as session:
        name = await session.scalar(text("SELECT name FROM traces"))
    assert name == "hijack-attempt"


async def test_each_key_resolves_to_its_own_project(
    tenants: tuple[Tenant, Tenant], client: object
) -> None:
    a, b = tenants

    for tenant, label in ((a, "from-a"), (b, "from-b")):
        response = await client.post(  # type: ignore[attr-defined]
            "/v1/ingest",
            json={"traces": [{"id": str(uuid4()), "name": label}]},
            headers=tenant.headers,
        )
        assert response.status_code == 202

    async with tenant_session(a.project_id) as session:
        assert list(await session.scalars(text("SELECT name FROM traces"))) == ["from-a"]
    async with tenant_session(b.project_id) as session:
        assert list(await session.scalars(text("SELECT name FROM traces"))) == ["from-b"]


async def test_unknown_key_is_rejected(client: object) -> None:
    response = await client.post(  # type: ignore[attr-defined]
        "/v1/ingest",
        json={"traces": []},
        headers={"Authorization": f"Bearer sk-test-{uuid4().hex}"},
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


async def test_revoked_key_stops_working(
    tenants: tuple[Tenant, Tenant], client: object, admin_engine: object
) -> None:
    """Deleting the project invalidates its key immediately.

    Authentication is a live lookup against `projects` rather than anything
    cached, so there is no window in which a removed tenant can still write.
    """
    a, _ = tenants

    response = await client.post(  # type: ignore[attr-defined]
        "/v1/ingest", json={"traces": []}, headers=a.headers
    )
    assert response.status_code == 202

    async with admin_engine.begin() as conn:  # type: ignore[attr-defined]
        await conn.execute(text("DELETE FROM projects WHERE id = :i"), {"i": a.project_id})

    response = await client.post(  # type: ignore[attr-defined]
        "/v1/ingest", json={"traces": []}, headers=a.headers
    )
    assert response.status_code == 401
