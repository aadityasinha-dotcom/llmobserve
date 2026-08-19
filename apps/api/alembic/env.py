import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.models import Base  # noqa: F401  - imported for metadata registration

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the migration connection URL.

    Migrations run as the schema owner, which is a different (more privileged)
    role than the one the API uses at runtime, so MIGRATION_DATABASE_URL wins
    over DATABASE_URL when both are present.
    """
    url = os.getenv("MIGRATION_DATABASE_URL")
    if url:
        return url
    settings = get_settings()
    return settings.migration_database_url or settings.database_url


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    """Keep autogenerate away from objects it cannot model.

    Daily partitions of `observations` are created by a maintenance function,
    not by migrations. Without this filter every autogenerate run would try to
    drop each of them.
    """
    if type_ == "table" and name.startswith("observations_p"):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
