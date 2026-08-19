"""initial schema: projects, traces, observations

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-17

Notes on things autogenerate cannot express, all of which are written by hand
below:

* `observations` is RANGE partitioned by day on `started_at`.
* Daily partitions plus a DEFAULT partition are created here, and a maintenance
  function `ensure_observations_partitions()` creates future ones.
* Row-level security is enabled and FORCEd on the tenant tables, and the
  restricted application role is granted only what it needs.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The non-superuser role the API connects as. Must match APP_DB_USER in
# docker-compose / deployment config.
APP_ROLE = "llmobserve_app"

# Number of days of partitions to pre-create ahead of the migration date.
PARTITION_LEAD_DAYS = 30

# Session GUC carrying the authenticated tenant. Set per transaction by
# app.deps.get_tenant_session.
TENANT_GUC = "app.current_project_id"

_TENANT_PREDICATE = f"project_id = nullif(current_setting('{TENANT_GUC}', true), '')::uuid"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # projects
    # ------------------------------------------------------------------
    op.create_table(
        "projects",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("api_key_hash", sa.String(64), nullable=False),
        sa.Column("api_key_prefix", sa.String(16), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_projects"),
        # Serves authentication: one indexed lookup per request.
        sa.UniqueConstraint("api_key_hash", name="uq_projects_api_key_hash"),
    )

    # ------------------------------------------------------------------
    # traces
    # ------------------------------------------------------------------
    op.create_table(
        "traces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("user_id", sa.String(255), nullable=True),
        sa.Column("session_id", sa.String(255), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sdk_version", sa.String(64), nullable=True),
        # project_id leads the PK so the tenancy predicate is an index prefix.
        sa.PrimaryKeyConstraint("project_id", "id", name="pk_traces"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_traces_project_id_projects",
            ondelete="CASCADE",
        ),
    )

    # The trace list view: newest traces for a project. The one read query that
    # is certain to matter.
    op.create_index(
        "ix_traces_project_id_started_at",
        "traces",
        ["project_id", sa.text("started_at DESC")],
    )

    # ------------------------------------------------------------------
    # observations - partitioned by day on started_at
    # ------------------------------------------------------------------
    op.create_table(
        "observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        # No FK to traces: batches split, reorder and retry independently, and
        # an FK would turn late trace delivery into rejected observations.
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_observation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "type", sa.String(32), server_default=sa.text("'span'"), nullable=False
        ),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "total_tokens",
            sa.Integer(),
            sa.Computed(
                "coalesce(prompt_tokens, 0) + coalesce(completion_tokens, 0)",
                persisted=True,
            ),
            nullable=True,
        ),
        # Numeric, never float. Frozen at write time from the price then in
        # force; never recomputed from current prices.
        sa.Column("cost_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("level", sa.String(16), nullable=True),
        sa.Column("status_message", sa.String(1024), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # started_at is in the PK because Postgres requires the partition key in
        # every unique constraint. Consequence: idempotency is keyed on
        # (project_id, id, started_at), so the SDK must reuse the original
        # start timestamp when it retries.
        sa.PrimaryKeyConstraint("project_id", "id", "started_at", name="pk_observations"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_observations_project_id_projects",
            ondelete="CASCADE",
        ),
        postgresql_partition_by="RANGE (started_at)",
    )

    # The trace detail view: every observation of one trace, in order.
    op.create_index(
        "ix_observations_project_id_trace_id_started_at",
        "observations",
        ["project_id", "trace_id", "started_at"],
    )

    # ------------------------------------------------------------------
    # Partition maintenance
    # ------------------------------------------------------------------
    # A DEFAULT partition means a missing daily partition degrades to a slow
    # write rather than a failed one - ingest is the fast path and dropping
    # data there is not acceptable. The cost is that attaching a new partition
    # whose range overlaps rows already sitting in the default will fail, so
    # ensure_observations_partitions() checks for that and warns instead of
    # erroring. Alert on a non-empty default partition.
    op.execute(
        """
        CREATE TABLE observations_default PARTITION OF observations DEFAULT;
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION ensure_observations_partitions(days_ahead integer DEFAULT 30,
                                                                 days_behind integer DEFAULT 1)
        RETURNS integer
        LANGUAGE plpgsql
        AS $$
        DECLARE
            day          date;
            part_name    text;
            created      integer := 0;
            stranded     bigint;
        BEGIN
            FOR day IN
                SELECT generate_series(
                    current_date - days_behind,
                    current_date + days_ahead,
                    interval '1 day'
                )::date
            LOOP
                part_name := format('observations_p%s', to_char(day, 'YYYYMMDD'));

                -- to_regclass rather than a bare pg_class lookup: an unqualified
                -- relname match in some other schema would silently skip
                -- creating the partition and strand the day's rows in DEFAULT.
                CONTINUE WHEN to_regclass(format('public.%I', part_name)) IS NOT NULL;

                -- Rows already routed to the default partition block the
                -- attach. Warn loudly rather than aborting the whole run.
                EXECUTE format(
                    'SELECT count(*) FROM observations_default WHERE started_at >= %L AND started_at < %L',
                    day, day + 1
                ) INTO stranded;

                IF stranded > 0 THEN
                    RAISE WARNING
                        'skipping % : % row(s) for that day are stranded in observations_default',
                        part_name, stranded;
                    CONTINUE;
                END IF;

                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF observations FOR VALUES FROM (%L) TO (%L)',
                    part_name, day, day + 1
                );
                created := created + 1;
            END LOOP;

            RETURN created;
        END;
        $$;
        """
    )

    op.execute(f"SELECT ensure_observations_partitions({PARTITION_LEAD_DAYS}, 1);")

    # ------------------------------------------------------------------
    # Row-level security
    # ------------------------------------------------------------------
    # FORCE is required: without it the table owner (which is who runs
    # migrations and who owns these tables) is exempt from its own policies.
    # Superusers are exempt regardless, which is why the API connects as a
    # separate, restricted role.
    #
    # The predicate fails closed: with the GUC unset, current_setting returns
    # NULL, the comparison is NULL, and no row is visible.
    for table in ("traces", "observations"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table}
                USING ({_TENANT_PREDICATE})
                WITH CHECK ({_TENANT_PREDICATE});
            """
        )

    # `projects` is deliberately not under RLS: it is the table authentication
    # resolves against, and the tenant is unknown until after that lookup.
    # SELECT-only for the app role limits the blast radius.
    op.execute(f"GRANT SELECT ON projects TO {APP_ROLE};")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON traces TO {APP_ROLE};")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON observations TO {APP_ROLE};")
    # Privileges on a partitioned table are checked on the parent when rows are
    # accessed through it, so future partitions need no additional grants.


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS ensure_observations_partitions(integer, integer);")
    op.drop_index("ix_observations_project_id_trace_id_started_at", table_name="observations")
    # Dropping the parent drops every partition with it.
    op.drop_table("observations")
    op.drop_index("ix_traces_project_id_started_at", table_name="traces")
    op.drop_table("traces")
    op.drop_table("projects")
