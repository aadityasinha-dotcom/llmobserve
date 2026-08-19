"""schedule daily partition maintenance via pg_cron, when available

Revision ID: 0002_partition_cron
Revises: 0001_initial
Create Date: 2026-08-17

Migration 0001 created `ensure_observations_partitions()` but nothing that calls
it on a schedule. Without a scheduler, the pre-created window of daily
partitions silently runs out and every write lands in observations_default.

This migration is conditional on pg_cron being installed, so it is a no-op on a
plain Docker Postgres and does the right thing on Supabase (where pg_cron is
enabled from the dashboard). It must stay a no-op rather than a failure: the
same migration runs against both targets.

On a target without pg_cron, call ensure_observations_partitions() from a
sidecar, a Kubernetes CronJob, or the API at startup.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_partition_cron"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_NAME = "observations-partitions"

# 03:00 UTC daily. Any time works as long as it leaves more than a day of slack
# before the pre-created window runs out - the window is 30 days, so a few
# consecutive failed runs are survivable without data landing in DEFAULT.
SCHEDULE = "0 3 * * *"


def upgrade() -> None:
    op.execute(
        f"""
        DO $do$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
                RAISE NOTICE
                    'pg_cron not installed; skipping the % job. Partition '
                    'maintenance must be scheduled externally - call '
                    'ensure_observations_partitions() at least weekly.',
                    '{JOB_NAME}';
                RETURN;
            END IF;

            -- Make re-running safe: cron.schedule would otherwise stack
            -- duplicate jobs under the same name across environments.
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


def downgrade() -> None:
    op.execute(
        f"""
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron')
               AND EXISTS (SELECT 1 FROM cron.job WHERE jobname = '{JOB_NAME}') THEN
                PERFORM cron.unschedule('{JOB_NAME}');
            END IF;
        END
        $do$;
        """
    )
