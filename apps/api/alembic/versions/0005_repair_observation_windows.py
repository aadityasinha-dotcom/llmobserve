"""recompute trace observation windows from the observations themselves

Revision ID: 0005_repair_windows
Revises: 0004_trace_obs_window
Create Date: 2026-09-16

Repairs rows written between 0004 and the fix to `_insert_stub_traces`.

That path created a placeholder trace with ON CONFLICT DO NOTHING, which was
right for the descriptive columns and wrong for the observation window: a client
flushing one observation per request (llm_metrics with flush_at=1, and any
streaming client) created the stub on the first observation and had every later
one discarded by the conflict clause. The stored window stayed one instant wide
while the trace's own spans fell outside it - so the detail endpoint, which
bounds its partition scan by that window, returned a subset of the observations
the list endpoint counted.

The write path now widens the window on conflict. This migration fixes the rows
already on disk. It is the same recomputation 0004 performed and is safe to run
repeatedly: it derives both bounds from `observations` rather than adjusting
what is stored, so a correct row is rewritten to the identical value.

RLS is lifted around the statement for the reason given in 0004 - migration 0001
sets FORCE, which subjects the owner to the tenant policies, and no
app.current_project_id is set here.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_repair_windows"
down_revision: str | None = "0004_trace_obs_window"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
               AND t.id = w.trace_id
               -- Only rows whose stored window actually disagrees with the
               -- observations, so a healthy table is not rewritten wholesale.
               AND (t.observations_started_min IS DISTINCT FROM w.min_started
                 OR t.observations_started_max IS DISTINCT FROM w.max_started);
            """
        )
    finally:
        for table in ("traces", "observations"):
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")


def downgrade() -> None:
    # Nothing to undo: this only corrects values to what the data already says.
    pass
