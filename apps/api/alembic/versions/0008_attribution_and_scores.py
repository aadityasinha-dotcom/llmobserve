"""attribution columns, token detail, and the scores table

Revision ID: 0008_attribution_scores
Revises: 0007_users_memberships
Create Date: 2026-09-26

Matches llm-metrics SDK 0.1.0, which started sending three kinds of data this
schema had no home for:

1. **Attribution on traces.** `tags`, `environment` and `release`. These are
   what turn a token ledger into something that answers "which feature", "which
   tenant" and "which deploy" - the questions a per-API-key bill cannot. Each is
   indexed with project_id leading, as every tenant-scoped index here is.
   `tags` gets a GIN index so `tags @> ARRAY['beta']` can use it.

2. **Token detail on observations.** `cached_tokens` and `reasoning_tokens` are
   subsets of prompt_tokens and completion_tokens respectively, and providers
   price the cached subset at a fraction of the fresh rate. Storing them lets
   `cost_usd` be right for cached traffic instead of over-reported. `prompt_name`
   and `prompt_version` let cost and quality be compared across versions of a
   prompt. All nullable: older SDKs never send them. ALTER on the partitioned
   parent propagates to every partition, DEFAULT included.

3. **`scores`.** A judgement about a trace or observation - a thumbs-down from a
   user, a heuristic check, a judge model's rating. This is the loop a billing
   page cannot close.

   Values arrive as a number, a boolean or a short label. They are stored in
   two columns: `value` (numeric, with booleans as 1/0 so they average) and
   `value_text` (the label), discriminated by `data_type`. Storing the raw
   value as JSONB would make "mean score per prompt version" a cast in every
   aggregate, and casting text to numeric in a GROUP BY is how dashboards get
   slow.

   Idempotent on (project_id, id) with ON CONFLICT DO NOTHING, same as
   observations: a score records something that already happened. No foreign
   keys to traces or observations, for the same reason those tables have none
   between them: a score for a trace whose batch is still in flight must not be
   rejected. `source` is free text rather than a CHECK, because rule 5 says an
   unrecognised value from a newer SDK is stored, not refused.

   Under row-level security like every other tenant table, and the app role
   gets SELECT and INSERT only. There is no UPDATE path for a score.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_attribution_scores"
down_revision: str | None = "0007_users_memberships"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "llmobserve_app"
TENANT_GUC = "app.current_project_id"
_TENANT_PREDICATE = f"project_id = nullif(current_setting('{TENANT_GUC}', true), '')::uuid"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # traces: attribution
    # ------------------------------------------------------------------
    op.add_column(
        "traces",
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    op.add_column("traces", sa.Column("environment", sa.String(64), nullable=True))
    op.add_column("traces", sa.Column("release", sa.String(128), nullable=True))

    # Equality column after project_id, then the keyset the list walks, so a
    # filtered page seeks straight to its first row like the name index does.
    for column in ("environment", "release", "session_id"):
        op.execute(
            f"""
            CREATE INDEX ix_traces_project_id_{column}_started_at_id
                ON traces (project_id, {column}, started_at DESC, id DESC)
                WHERE {column} IS NOT NULL;
            """
        )
    op.execute("CREATE INDEX ix_traces_tags ON traces USING gin (tags);")

    # ------------------------------------------------------------------
    # observations: token detail and prompt attribution
    # ------------------------------------------------------------------
    op.add_column("observations", sa.Column("cached_tokens", sa.Integer(), nullable=True))
    op.add_column("observations", sa.Column("reasoning_tokens", sa.Integer(), nullable=True))
    op.add_column("observations", sa.Column("prompt_name", sa.String(255), nullable=True))
    op.add_column("observations", sa.Column("prompt_version", sa.String(64), nullable=True))

    # ------------------------------------------------------------------
    # scores
    # ------------------------------------------------------------------
    op.create_table(
        "scores",
        # Client-supplied; the idempotency key.
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        # numeric | boolean | categorical
        sa.Column("data_type", sa.String(16), nullable=False),
        sa.Column("value", sa.Numeric(18, 6), nullable=True),
        sa.Column("value_text", sa.String(255), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        # human | llm_judge | heuristic, unconstrained on purpose.
        sa.Column("source", sa.String(32), nullable=False, server_default=sa.text("'human'")),
        sa.Column(
            "metadata", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        # Event time as reported by the SDK, distinct from server receive time.
        sa.Column("scored_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("project_id", "id", name="pk_scores"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_scores_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "trace_id IS NOT NULL OR observation_id IS NOT NULL",
            name="ck_scores_has_target",
        ),
        sa.CheckConstraint(
            "data_type IN ('numeric', 'boolean', 'categorical')",
            name="ck_scores_data_type_known",
        ),
    )
    # The two lookups the dashboard makes: every score of one trace (the detail
    # view), and one score name over time (the quality chart).
    op.create_index("ix_scores_project_id_trace_id", "scores", ["project_id", "trace_id"])
    op.create_index(
        "ix_scores_project_id_observation_id", "scores", ["project_id", "observation_id"]
    )
    op.execute(
        """
        CREATE INDEX ix_scores_project_id_name_scored_at
            ON scores (project_id, name, scored_at DESC);
        """
    )

    op.execute("ALTER TABLE scores ENABLE ROW LEVEL SECURITY;")
    op.execute("ALTER TABLE scores FORCE ROW LEVEL SECURITY;")
    op.execute(
        f"""
        CREATE POLICY scores_tenant_isolation ON scores
            USING ({_TENANT_PREDICATE})
            WITH CHECK ({_TENANT_PREDICATE});
        """
    )
    op.execute(f"GRANT SELECT, INSERT ON scores TO {APP_ROLE};")


def downgrade() -> None:
    op.drop_table("scores")

    for column in ("prompt_version", "prompt_name", "reasoning_tokens", "cached_tokens"):
        op.drop_column("observations", column)

    op.execute("DROP INDEX IF EXISTS ix_traces_tags;")
    for column in ("session_id", "release", "environment"):
        op.execute(f"DROP INDEX IF EXISTS ix_traces_project_id_{column}_started_at_id;")
    op.drop_column("traces", "release")
    op.drop_column("traces", "environment")
    op.drop_column("traces", "tags")
