from datetime import datetime
from uuid import UUID

from sqlalchemy import String, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TIMESTAMP

from app.models.base import Base


class Project(Base):
    """Tenant root. Resolved from the API key on every request."""

    __tablename__ = "projects"

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # sha256 hex of the raw key. Raw keys are shown once at creation and never
    # stored. Unindexed lookups here would be on the request hot path, so this
    # column carries the unique index that serves authentication.
    api_key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # First few characters of the raw key, for display ("which key is this?").
    # Not a secret and not sufficient to authenticate.
    api_key_prefix: Mapped[str | None] = mapped_column(String(16), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )
