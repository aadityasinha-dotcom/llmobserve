"""widen the partition window behind current_date to cover the accepted backfill

Revision ID: 0003_partition_backfill
Revises: 0002_partition_cron
Create Date: 2026-08-19

Migrations 0001 and 0002 maintain daily partitions from current_date - 1 to
current_date + 30. The API accepts observations up to
`ingest_max_event_age_days` old (default 7), so any event delivered more than a
day late had no partition and landed in observations_default.

That is worse than it sounds. ensure_observations_partitions() refuses to create
a partition for a day whose rows are already stranded in DEFAULT - it warns and
skips, by design, because CREATE TABLE ... PARTITION OF would otherwise error
and abort the whole maintenance run. So a single late event permanently costs
that day its partition, and every subsequent write for that day also goes to
DEFAULT and stops being pruned.

This migration makes the storage window a superset of the accepted window:

    accepted   [ current_date - 7, current_date + 1 ]   (app/config.py)
    partitions [ current_date - 8, current_date + 30 ]  (here)

The extra day behind and the wide margin ahead exist so that neither a clock
skew at the boundary nor a stretch of failed cron runs can strand anything.
Keep these two in step: widening ingest_max_event_age_days without widening
days_behind here reintroduces exactly the bug this fixes.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_partition_backfill"
down_revision: str | None = "0002_partition_cron"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_NAME = "observations-partitions"
SCHEDULE = "0 3 * * *"

# Must exceed app.config.Settings.ingest_max_event_age_days.
DAYS_BEHIND = 8
DAYS_AHEAD = 30


def upgrade() -> None:
    # Backfill the missing days now. Safe to run against a populated table: the
    # function skips any day whose rows are already stranded rather than failing.
    op.execute(f"SELECT ensure_observations_partitions({DAYS_AHEAD}, {DAYS_BEHIND});")

    # Point the scheduled job at the wider window too, so it keeps up.
    op.execute(
        f"""
        DO $do$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
                RAISE NOTICE 'pg_cron not installed; nothing to reschedule.';
                RETURN;
            END IF;

            IF EXISTS (SELECT 1 FROM cron.job WHERE jobname = '{JOB_NAME}') THEN
                PERFORM cron.unschedule('{JOB_NAME}');
            END IF;

            PERFORM cron.schedule(
                '{JOB_NAME}',
                '{SCHEDULE}',
                $cron$SELECT ensure_observations_partitions({DAYS_AHEAD}, {DAYS_BEHIND})$cron$
            );
        END
        $do$;
        """
    )


def downgrade() -> None:
    # Only the schedule is reverted. Dropping the extra partitions would delete
    # the rows in them, which a downgrade has no business doing.
    op.execute(
        f"""
        DO $do$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
                RETURN;
            END IF;

            IF EXISTS (SELECT 1 FROM cron.job WHERE jobname = '{JOB_NAME}') THEN
                PERFORM cron.unschedule('{JOB_NAME}');
            END IF;

            PERFORM cron.schedule(
                '{JOB_NAME}',
                '{SCHEDULE}',
                $cron$SELECT ensure_observations_partitions(30, 1)$cron$
            );
        END
        $do$;
        """
    )
