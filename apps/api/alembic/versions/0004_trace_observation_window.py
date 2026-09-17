"""record each trace's observation time window, and index it for the list view

Revision ID: 0004_trace_obs_window
Revises: 0003_partition_backfill
Create Date: 2026-09-16

Two changes, both in service of GET /v1/traces.

1. `traces.observations_started_min` / `observations_started_max`.

   `observations` is RANGE partitioned by day on started_at, and nothing drops
   old partitions - the count grows by one per day forever. An aggregate that
   does not constrain started_at therefore touches every partition that has ever
   existed, and gets slower every day the service runs.

   The window cannot be derived from what was already stored. `started_at` on a
   trace is overwritten by the ingest upsert, `ended_at` is NULL while a trace is
   open, and a long-running agent trace can emit observations days after it
   began. Any interval guessed from those columns silently undercounts tokens and
   cost, which is the worst way for a cost dashboard to be wrong. Storing the
   true bound at write time is the only version that is exact.

   Maintained by the ingest upsert with least()/greatest(), which ignore NULLs
   in Postgres, so the merge stays idempotent under replay and costs no extra
   round trip on the fast path.

2. The trace-list indexes gain `id DESC`.

   Cursor pagination orders by (started_at DESC, id DESC) and seeks with a row
   comparison on the same pair. Without `id` in the index the seek lands on the
   timestamp and re-filters, which matters exactly when it is least affordable:
   a batch that writes hundreds of traces sharing one millisecond.

The backfill runs with FORCE ROW LEVEL SECURITY lifted. Migration 0001 sets
FORCE, which subjects even the table owner to the tenant policies, and the
owner running this migration has no `app.current_project_id` set - so without
lifting it the UPDATE would match zero rows and report success. Restored before
the migration ends.

Creating the indexes non-concurrently holds a write lock on `traces` for the
duration. That is the right trade at this size; against a large production table
use CREATE INDEX CONCURRENTLY outside a transaction instead.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_trace_obs_window"
down_revision: str | None = "0003_partition_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Superseded by the id-tiebroken versions below.
_OLD_STARTED_INDEX = "ix_traces_project_id_started_at"

_KEYSET_INDEX = "ix_traces_project_id_started_at_id"
_NAME_KEYSET_INDEX = "ix_traces_project_id_name_started_at_id"


def upgrade() -> None:
    op.add_column(
        "traces",
        sa.Column("observations_started_min", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "traces",
        sa.Column("observations_started_max", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    # See the module docstring: the owner is subject to its own policies here.
    for table in ("traces", "observations"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
    try:
        op.execute(
            """
            UPDATE traces t
               SET observations_started_min = w.min_started,
                   observations_started_max = w.max_started
              FROM (
                    SELECT project_id, trace_id,
                           min(started_at) AS min_started,
                           max(started_at) AS max_started
                      FROM observations
                     GROUP BY project_id, trace_id
                   ) w
             WHERE t.project_id = w.project_id
               AND t.id = w.trace_id;
            """
        )
    finally:
        for table in ("traces", "observations"):
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")

    # Replace the list index with one that carries the cursor tiebreaker.
    op.drop_index(_OLD_STARTED_INDEX, table_name="traces")
    op.create_index(
        _KEYSET_INDEX,
        "traces",
        ["project_id", sa.text("started_at DESC"), sa.text("id DESC")],
    )
    # The ?name= filter is exact-match, so it belongs ahead of the sort columns:
    # equality first, then the ordering the keyset walks.
    op.create_index(
        _NAME_KEYSET_INDEX,
        "traces",
        ["project_id", "name", sa.text("started_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_index(_NAME_KEYSET_INDEX, table_name="traces")
    op.drop_index(_KEYSET_INDEX, table_name="traces")
    op.create_index(
        _OLD_STARTED_INDEX,
        "traces",
        ["project_id", sa.text("started_at DESC")],
    )
    op.drop_column("traces", "observations_started_max")
    op.drop_column("traces", "observations_started_min")
