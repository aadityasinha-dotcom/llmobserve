from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Computed,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TIMESTAMP

from app.models.base import Base


class Observation(Base):
    """A single step inside a trace: an LLM generation, a span, or an event.

    RANGE partitioned by day on `started_at`. Two consequences worth knowing
    before writing queries against this table:

    1. Every unique constraint must contain the partition key, so the strongest
       available idempotency key is (project_id, id, started_at) rather than id
       alone. A retry that regenerates its start timestamp WILL duplicate. The
       SDK must capture start_time once and reuse it across retries.
    2. Queries that do not constrain started_at touch every partition. Always
       pass a time range on read paths.
    """

    __tablename__ = "observations"
    __table_args__ = (
        PrimaryKeyConstraint("project_id", "id", "started_at", name="pk_observations"),
        # The trace detail view: every observation of one trace, in order.
        Index(
            "ix_observations_project_id_trace_id_started_at",
            "project_id",
            "trace_id",
            "started_at",
        ),
        {"postgresql_partition_by": "RANGE (started_at)"},
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    # Denormalised from the parent trace so that RLS, the primary key and every
    # secondary index can lead with it. Worth the redundancy.
    project_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    # No foreign key to traces on purpose. Batches split, arrive out of order
    # and retry independently; an FK would turn late trace delivery into
    # rejected observations, i.e. silent data loss on the fast path. The router
    # upserts a stub trace row instead.
    trace_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)

    # Self-reference for nesting. Also unenforced - the parent may live in a
    # different daily partition, and Postgres cannot express that FK cheaply.
    parent_observation_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    # span | generation | event. Left unconstrained: a newer SDK sending an
    # unknown type must not 4xx or 500.
    type: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'span'"))
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)

    input: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Subsets of the two counts above, not additions to them. prompt_tokens is
    # always the whole prompt and cached_tokens the part served from the
    # provider's prompt cache; the SDK normalises providers that report it the
    # other way round. Priced separately - see services/pricing.py.
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Generated column: derived in the database so no writer can disagree with
    # the parts it was derived from.
    total_tokens: Mapped[int | None] = mapped_column(
        Integer,
        Computed("coalesce(prompt_tokens, 0) + coalesce(completion_tokens, 0)", persisted=True),
        nullable=True,
    )

    # Numeric, never float. Computed server-side at write time from the price
    # in force then, and never recomputed from current prices.
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)

    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Which prompt template produced this call, so cost and quality can be
    # compared across versions. Free text; the prompts table is a later phase.
    prompt_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    observation_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # Partition key.
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
