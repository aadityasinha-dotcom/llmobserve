"""drop the single-key columns from projects

Revision ID: 0009_drop_project_key_columns
Revises: 0008_attribution_scores
Create Date: 2026-11-23

The contract half of 0006. That migration moved keys into `api_keys` and left
`projects.api_key_hash` / `api_key_prefix` in place, nullable, so an instance
still on the old code could authenticate during the rollout. Every instance
has read `api_keys` since then; nothing in the API, the scripts or the tests
has referenced these columns for two months, and `auth_sign_in_google` creates
projects without them. Keeping them around is now only a way to confuse the
next reader into thinking there are two places a key can live.

Nothing is lost: 0006 copied every hash into `api_keys` with the label
`migrated`, and the downgrade below restores the columns from the oldest
surviving key per project, which is that same row.

Safe in either order with a deploy. The ORM model no longer names the columns,
so the current code works before and after this runs, and the previous code
only ever read them when the row had a value.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_drop_project_key_columns"
down_revision: str | None = "0008_attribution_scores"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_projects_api_key_hash", "projects", type_="unique")
    op.drop_column("projects", "api_key_prefix")
    op.drop_column("projects", "api_key_hash")


def downgrade() -> None:
    # Nullable here; 0006's downgrade is what tightens it back to NOT NULL.
    op.add_column("projects", sa.Column("api_key_hash", sa.String(64), nullable=True))
    op.add_column("projects", sa.Column("api_key_prefix", sa.String(16), nullable=True))
    op.execute(
        """
        UPDATE projects p
           SET api_key_hash = k.key_hash,
               api_key_prefix = k.key_prefix
          FROM (
                SELECT DISTINCT ON (project_id) project_id, key_hash, key_prefix
                  FROM api_keys
                 WHERE revoked_at IS NULL
                 ORDER BY project_id, created_at
               ) k
         WHERE p.id = k.project_id;
        """
    )
    op.create_unique_constraint("uq_projects_api_key_hash", "projects", ["api_key_hash"])
