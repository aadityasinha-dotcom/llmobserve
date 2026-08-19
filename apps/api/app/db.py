import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import get_settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _engine_kwargs() -> dict[str, Any]:
    """Engine configuration, which differs materially by pooling mode."""
    settings = get_settings()

    if settings.db_pool_mode == "transaction":
        # Behind Supavisor transaction mode / pgbouncer the server connection is
        # handed to another client between our transactions, so:
        #   * NullPool - SQLAlchemy must not hold connections; the pooler owns
        #     their lifecycle. This is also what keeps a horizontally scaled
        #     Cloud Run deployment from exhausting the project's connection cap.
        #   * statement_cache_size=0 - asyncpg must not assume a prepared
        #     statement it created earlier still exists on this connection.
        #   * prepared_statement_name_func - even with caching off, asyncpg names
        #     some statements internally; unique names avoid colliding with
        #     another client's statement of the same name.
        return {
            "poolclass": NullPool,
            "connect_args": {
                "statement_cache_size": 0,
                "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
            },
        }

    # A connection we own for its lifetime. Prepared statements are safe here
    # and worth keeping on the ingest fast path.
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_pre_ping": True,
    }


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.db_echo,
            **_engine_kwargs(),
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)
    return _sessionmaker


async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a session with no tenant context set.

    Only safe for tables that are not under row-level security - in practice
    just `projects`, during authentication. Tenant-scoped work must go through
    `app.deps.get_tenant_session`.
    """
    async with get_sessionmaker()() as session:
        yield session


_RLS_BYPASS_SQL = text(
    """
    SELECT coalesce(bool_or(rolsuper OR rolbypassrls), false)
    FROM pg_roles
    WHERE rolname = current_user
    """
)


async def rls_is_bypassed(conn: AsyncConnection) -> bool:
    """Report whether the runtime role is exempt from row-level security.

    This exists because the failure it detects is silent. Every tenant policy
    can be correctly defined and still enforce nothing if the application
    happens to connect as a superuser or a BYPASSRLS role - queries succeed,
    return data, and quietly ignore the policies.

    The realistic way to end up there is to paste the connection string a
    managed provider offers by default. Supabase hands you one for its `postgres`
    role; using it as DATABASE_URL disables tenant isolation with no error
    anywhere. A single-tenant test cannot tell the difference either, so this
    check is the thing standing between that mistake and production.
    """
    return bool(await conn.scalar(_RLS_BYPASS_SQL))


async def check_database_ready() -> None:
    """Verify the database is reachable *and* that tenant isolation is real.

    Raises RuntimeError when RLS would be bypassed, so an instance in that state
    fails its readiness probe and never receives traffic.
    """
    settings = get_settings()

    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))

        if not await rls_is_bypassed(conn):
            return

        current_user = await conn.scalar(text("SELECT current_user"))

    message = (
        f"Runtime database role {current_user!r} can bypass row-level security. "
        "Tenant isolation is NOT being enforced. Point DATABASE_URL at the "
        "restricted application role instead of an admin/superuser role."
    )

    if settings.db_require_rls:
        raise RuntimeError(message)
    logger.critical("%s (db_require_rls is disabled)", message)


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
