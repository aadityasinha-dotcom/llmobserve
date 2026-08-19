from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, PrimaryKeyConstraint, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TIMESTAMP

from app.models.base import Base


class Trace(Base):
    """One SDK trace: a single logical run through the customer's application."""

    __tablename__ = "traces"
    __table_args__ = (
        # project_id leads so the tenancy predicate is an index prefix rather
        # than a post-scan filter, and so RLS costs nothing extra.
        PrimaryKeyConstraint("project_id", "id", name="pk_traces"),
        # The trace list view: most recent traces for a project.
        Index("ix_traces_project_id_started_at", "project_id", text("started_at DESC")),
    )

    # Client-supplied. This is the idempotency key: a retried batch carries the
    # same id and lands as ON CONFLICT DO NOTHING.
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    project_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The customer's own end-user identifier, not a user of this platform.
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    trace_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # Event time, as reported by the SDK.
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    # Server receive time. Deliberately distinct from started_at: clients batch,
    # buffer and retry, so the gap between the two is real and worth measuring.
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )

    # From the X-SDK-Version header. Stored so deprecation decisions have data.
    sdk_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
