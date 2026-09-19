"""Shared fixtures.

These tests run against a real Postgres. That is not incidental: the property
under test in test_tenant_isolation.py is enforced by Postgres row-level
security, so a mocked session or an in-memory SQLite would assert nothing. The
whole point is that isolation holds even when the application layer is wrong.

Point DATABASE_URL at the restricted application role and MIGRATION_DATABASE_URL
(or SUPABASE_MIGRATION_URL) at the owner. The first is what the app uses and
what RLS is evaluated against; the second exists only to seed and tear down
`projects`, which the application role can read but not write.
"""

import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

# Load the nearest .env before importing anything under app., because
# app.config.get_settings() is lru_cached and captures the environment on its
# first call. Real environment variables win, via setdefault - inside the
# container compose has already set them and there is no .env on the path at
# all, which is why this walks up and tolerates finding nothing rather than
# indexing a fixed number of parents.
for _parent in Path(__file__).resolve().parents:
    _env_file = _parent / ".env"
    if _env_file.is_file():
        for _line in _env_file.read_text().splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _key, _, _value = _line.partition("=")
            os.environ.setdefault(_key.strip(), _value.strip())
        break

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.db import dispose_engine  # noqa: E402
from app.deps import get_tenant_session, hash_api_key  # noqa: E402
from app.main import app  # noqa: E402


@dataclass(frozen=True)
class Tenant:
    """A project plus the credentials that authenticate as it."""

    project_id: UUID
    api_key: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


async def _insert_key(
    conn: Any,
    project_id: UUID,
    raw_key: str,
    scopes: list[str],
    label: str | None = None,
    revoked: bool = False,
) -> None:
    await conn.execute(
        text(
            "INSERT INTO api_keys (project_id, key_hash, key_prefix, label, scopes, revoked_at) "
            "VALUES (:p, :h, :pre, :l, :s, CASE WHEN :rev THEN now() ELSE NULL END)"
        ),
        {
            "p": project_id,
            "h": hash_api_key(raw_key),
            "pre": raw_key[:16],
            "l": label,
            "s": scopes,
            "rev": revoked,
        },
    )


async def issue_key(
    admin_engine: Any,
    project_id: UUID,
    scopes: list[str],
    label: str | None = None,
    revoked: bool = False,
) -> str:
    """Mint an extra key for a project and return the raw value."""
    raw_key = f"sk-test-{uuid4().hex}"
    async with admin_engine.begin() as conn:
        await _insert_key(conn, project_id, raw_key, scopes, label, revoked)
    return raw_key


def _admin_url() -> str:
    for var in ("MIGRATION_DATABASE_URL", "SUPABASE_MIGRATION_URL"):
        url = os.environ.get(var)
        if url:
            return url
    pytest.skip(
        "No MIGRATION_DATABASE_URL / SUPABASE_MIGRATION_URL set; "
        "these tests need an owner connection to seed projects."
    )


@pytest.fixture(autouse=True)
async def _reset_app_engine() -> AsyncIterator[None]:
    """Dispose the application engine between tests.

    app.db caches the engine in a module global. Under session pooling those
    connections are bound to the event loop that created them, and pytest-asyncio
    gives each test its own loop - so without this, the second test to touch the
    database fails with a cross-loop error. Disposing also keeps a failed test
    from leaving a poisoned connection behind for the next one.
    """
    yield
    await dispose_engine()


@pytest.fixture
async def admin_engine() -> AsyncIterator[Any]:
    engine = create_async_engine(_admin_url())
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def tenants(admin_engine: Any) -> AsyncIterator[tuple[Tenant, Tenant]]:
    """Two unrelated projects, torn down afterwards.

    Each test gets fresh ids, so a test that leaves rows behind cannot influence
    another. Deleting the projects cascades to their traces and observations.
    """
    a = Tenant(uuid4(), f"sk-test-{uuid4().hex}")
    b = Tenant(uuid4(), f"sk-test-{uuid4().hex}")

    async with admin_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO projects (id, name) VALUES (:ia, 'test-A'), (:ib, 'test-B')"),
            {"ia": a.project_id, "ib": b.project_id},
        )
        # Full-scope keys: most tests are not about authorisation, and a narrow
        # default would make every unrelated test assert scopes it does not care
        # about. Scope-specific tests mint their own with issue_key().
        for tenant in (a, b):
            await _insert_key(conn, tenant.project_id, tenant.api_key, ["ingest", "read"], "test")
    try:
        yield a, b
    finally:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM projects WHERE id IN (:ia, :ib)"),
                {"ia": a.project_id, "ib": b.project_id},
            )


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@asynccontextmanager
async def tenant_session(project_id: UUID) -> AsyncIterator[AsyncSession]:
    """Drive the real `app.deps.get_tenant_session` dependency.

    Deliberately not a reimplementation. The behaviour under test is precisely
    what that dependency does - setting app.current_project_id transaction-locally
    so the RLS policies have something to compare against - so a test that issued
    its own set_config would be testing itself rather than the application.
    """
    agen = cast(AsyncGenerator[AsyncSession, None], get_tenant_session(project_id))
    session = await agen.__anext__()
    try:
        yield session
    except BaseException as exc:
        # Hand the failure back to the dependency so it unwinds exactly as it
        # would on a failed request: rollback, not commit. athrow re-raises
        # whatever comes back out; the original is re-raised below regardless.
        with suppress(BaseException):
            await agen.athrow(exc)
        raise
    else:
        with suppress(StopAsyncIteration):
            await agen.__anext__()


async def seed_trace(
    project_id: UUID,
    name: str,
    trace_id: UUID | None = None,
    with_observation: bool = False,
) -> UUID:
    """Insert one trace (optionally with an observation) as the given tenant."""
    trace_id = trace_id or uuid4()
    async with tenant_session(project_id) as session:
        await session.execute(
            text(
                "INSERT INTO traces (id, project_id, name, started_at) "
                "VALUES (:id, :pid, :name, now())"
            ),
            {"id": trace_id, "pid": project_id, "name": name},
        )
        if with_observation:
            await session.execute(
                text(
                    "INSERT INTO observations (id, project_id, trace_id, started_at) "
                    "VALUES (:id, :pid, :tid, now())"
                ),
                {"id": uuid4(), "pid": project_id, "tid": trace_id},
            )
    return trace_id
