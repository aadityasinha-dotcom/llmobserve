from functools import lru_cache
from typing import Any, Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _clean(value: Any) -> Any:
    """Strip whitespace and one matched pair of surrounding quotes.

    Configuration on a hosting platform is typed or pasted into a web form, and
    two things ride along: a trailing space or newline, and the quotes from a
    copied shell line. Both are invisible in the dashboard and neither is ever
    part of an intended value, but `DB_REQUIRE_RLS="true"` fails validation with
    a message about booleans - which on a serverless platform costs a redeploy
    to diagnose.

    Only matched quotes are removed, and only from the outside, so a value that
    genuinely contains a quote is untouched.
    """
    if not isinstance(value, str):
        return value
    cleaned = value.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _tidy_env_values(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return {key: _clean(value) for key, value in data.items()}

    environment: str = "development"
    log_level: str = "INFO"

    # Connection used by the application at runtime. Must point at the
    # restricted, non-superuser role so row-level security applies.
    database_url: str = (
        "postgresql+asyncpg://llmobserve_app:llmobserve_app@localhost:5432/llmobserve"
    )
    # Connection used by Alembic only. Owns the schema; bypasses RLS.
    migration_database_url: str | None = None

    redis_url: str = "redis://localhost:6379/0"

    # How the runtime connection reaches Postgres.
    #
    #   "session"     - a connection this process keeps: local Docker, a Supabase
    #                   direct connection, or the Supavisor session-mode port
    #                   (5432). SQLAlchemy owns the pool and prepared statements
    #                   are left enabled, which is worth real latency on the
    #                   ingest path.
    #   "transaction" - Supavisor transaction mode (6543) or any pgbouncer-style
    #                   pooler. The connection is only ours for the duration of a
    #                   transaction, so SQLAlchemy must not pool it and prepared
    #                   statements must be disabled.
    #
    # Getting this wrong against a transaction-mode pooler surfaces as
    # DuplicatePreparedStatementError under concurrency, not at startup.
    db_pool_mode: Literal["session", "transaction"] = "session"

    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_echo: bool = False

    # Refuse readiness if the runtime role can bypass row-level security.
    # Disable only if you have a deliberate reason; see app.db.rls_is_bypassed.
    db_require_rls: bool = True

    ingest_max_traces_per_batch: int = 1000
    ingest_max_observations_per_batch: int = 5000

    # How far outside the present an observation's started_at may fall before it
    # is refused. `observations` is RANGE partitioned by day, so a timestamp with
    # no matching partition lands in observations_default - and once a day's rows
    # are stranded there, ensure_observations_partitions() can never create that
    # day's partition (it warns and skips), so every later write for that day
    # goes to DEFAULT too and stops being pruned.
    #
    # Both bounds must stay inside the partition window maintained by migration
    # 0003, which runs from current_date - 8 to current_date + 30. The asymmetry
    # is deliberate: late delivery is normal (buffering, offline queues, retry
    # backoff) while a timestamp days in the future only ever means a broken
    # clock.
    ingest_max_event_age_days: int = 7
    ingest_max_event_future_days: int = 1


@lru_cache
def get_settings() -> Settings:
    return Settings()
