from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Index, PrimaryKeyConstraint, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
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
        # The trace list view, and the cursor that pages it. `id DESC` is the
        # keyset tiebreaker, not decoration: without it a seek into a batch of
        # traces sharing one timestamp re-filters instead of descending.
        Index(
            "ix_traces_project_id_started_at_id",
            "project_id",
            text("started_at DESC"),
            text("id DESC"),
        ),
        # The ?name= filter. Equality column first, then the ordering the
        # keyset walks.
        Index(
            "ix_traces_project_id_name_started_at_id",
            "project_id",
            "name",
            text("started_at DESC"),
            text("id DESC"),
        ),
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

    # Attribution. Free-form labels for slicing (feature, tenant, experiment
    # arm), and where the run happened, so a regression can be pinned to a
    # deploy. All client-supplied; the SDK stamps environment/release from its
    # own configuration.
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    environment: Mapped[str | None] = mapped_column(String(64), nullable=True)
    release: Mapped[str | None] = mapped_column(String(128), nullable=True)

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

    # The true started_at span of this trace's observations, maintained by the
    # ingest upsert with least()/greatest().
    #
    # These exist so a read can put a bound on `observations.started_at`, which
    # is that table's partition key. Without one, aggregating a page of traces
    # touches every daily partition ever created. They cannot be derived from
    # the columns above: `started_at` here is client-supplied and overwritten on
    # every re-send, `ended_at` is NULL while the trace is open, and a
    # long-running trace outlives its own start date. NULL means no observation
    # has arrived yet, which is a normal state, not a missing value.
    observations_started_min: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    observations_started_max: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
