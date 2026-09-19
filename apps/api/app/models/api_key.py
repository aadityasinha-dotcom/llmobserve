from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TIMESTAMP

from app.models.base import Base

#: What a key is allowed to do. `ingest` writes, `read` queries.
INGEST_SCOPE = "ingest"
READ_SCOPE = "read"
VALID_SCOPES = frozenset({INGEST_SCOPE, READ_SCOPE})


class ApiKey(Base):
    """One credential for one project.

    Separate from `projects` so a project can have several: an ingest-only key
    that ships inside a customer's process, a read-only key for the dashboard,
    one per environment. With a single key on the project there was no way to
    hand out write access without also handing over every stored prompt and
    completion.

    Deliberately outside row-level security, for the same reason `projects` is:
    this is the table authentication resolves against, so there is no tenant to
    compare a policy against until after the lookup succeeds. The application
    role holds SELECT and nothing else.
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint(
            "cardinality(scopes) > 0 AND scopes <@ ARRAY['ingest','read']::text[]",
            name="ck_api_keys_scopes_known",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    project_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    # sha256 hex of the raw key; the raw key is shown once and never stored.
    # Unique, and the index that serves authentication on every request.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # First characters of the raw key, so two keys are distinguishable in a
    # list. Not a secret, and not sufficient to authenticate.
    key_prefix: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # "sdk", "dashboard", "ci" - display only.
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    scopes: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("ARRAY['ingest','read']::text[]")
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    # Set, never deleted. The row must survive revocation so the hash cannot be
    # reissued and so the revocation stays auditable.
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
