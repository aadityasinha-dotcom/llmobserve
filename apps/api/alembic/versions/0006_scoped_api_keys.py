"""move API keys into their own table, with scopes and revocation

Revision ID: 0006_scoped_api_keys
Revises: 0005_repair_windows
Create Date: 2026-09-19

`projects.api_key_hash` allowed exactly one key per project, and that key could
do everything. Two consequences, both bad:

* The SDK and the dashboard had to share one credential. An ingest key handed to
  a client application could also read back every prompt and completion ever
  stored for that project - the wrong blast radius for a key that ships inside
  someone else's process.
* Rotating for one consumer broke the other, and revoking meant deleting the
  project or overwriting the hash.

`api_keys` fixes both: many keys per project, each carrying its own scopes and
its own `revoked_at`.

Expand, not contract. `projects.api_key_hash` is backfilled into this table and
then made nullable rather than dropped, so a deployment mid-rollout - an old
instance still reading the column while a new one reads the table - keeps
working, and a rollback has its data. Migration 0007 should drop the columns
once every instance is on the new code.

Existing keys are backfilled with BOTH scopes. They are in use by a live SDK and
dashboard right now, and a migration that silently narrowed them would revoke
access as a side effect of an upgrade. Narrowing is a deliberate act: issue a
scoped key and revoke the old one.

Like `projects`, this table is deliberately outside row-level security. It is
what authentication resolves against, so the tenant is unknown until after the
lookup - there is no project id to compare a policy against yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_scoped_api_keys"
down_revision: str | None = "0005_repair_windows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "llmobserve_app"

# The scopes a key may carry. Kept as a checked text[] rather than a Postgres
# enum: adding a scope later is an ALTER on the constraint instead of an enum
# migration, and the set is small enough that a check reads clearly.
VALID_SCOPES = ("ingest", "read")


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        # sha256 hex of the raw key. The raw key is shown once and never stored.
        sa.Column("key_hash", sa.String(64), nullable=False),
        # First characters of the raw key, so a human can tell two keys apart in
        # a list. Not a secret and not enough to authenticate with.
        sa.Column("key_prefix", sa.String(16), nullable=True),
        # What this key is for: "sdk", "dashboard", "ci". Display only.
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("ARRAY['ingest','read']::text[]"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Set, never deleted: a revoked key must stay in the table so its hash
        # cannot be reissued and so the revocation itself is auditable.
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
        # Serves authentication: one indexed lookup per request.
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_api_keys_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "cardinality(scopes) > 0 AND scopes <@ ARRAY['ingest','read']::text[]",
            name="ck_api_keys_scopes_known",
        ),
    )

    # Carry the existing keys over so nothing in flight stops working.
    op.execute(
        """
        INSERT INTO api_keys (project_id, key_hash, key_prefix, label, scopes)
        SELECT id, api_key_hash, api_key_prefix, 'migrated', ARRAY['ingest','read']::text[]
          FROM projects
         WHERE api_key_hash IS NOT NULL;
        """
    )

    # Deprecated, not dropped. See the module docstring.
    op.alter_column("projects", "api_key_hash", nullable=True)

    op.execute(f"GRANT SELECT ON api_keys TO {APP_ROLE};")


def downgrade() -> None:
    # Restore the single-key column from the oldest surviving key per project,
    # which is the one the backfill above created.
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
         WHERE p.id = k.project_id
           AND p.api_key_hash IS NULL;
        """
    )
    # A project with no surviving key cannot satisfy NOT NULL; drop it rather
    # than fail the migration, since it is unreachable without a key anyway.
    op.execute("DELETE FROM projects WHERE api_key_hash IS NULL;")
    op.alter_column("projects", "api_key_hash", nullable=False)
    op.drop_table("api_keys")
