from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TIMESTAMP

from app.models.base import Base

# How a score's value is stored. Booleans land in `value` as 1/0 so they can be
# averaged alongside numeric scores; labels land in `value_text`.
SCORE_NUMERIC = "numeric"
SCORE_BOOLEAN = "boolean"
SCORE_CATEGORICAL = "categorical"


class Score(Base):
    """A judgement about a trace or an observation.

    No foreign keys to either target, for the same reason observations carry
    none to traces: batches split and retry independently, and a score whose
    subject is still in flight must be stored, not rejected. A dangling target
    id is normal.
    """

    __tablename__ = "scores"
    __table_args__ = (
        PrimaryKeyConstraint("project_id", "id", name="pk_scores"),
        CheckConstraint(
            "trace_id IS NOT NULL OR observation_id IS NOT NULL", name="ck_scores_has_target"
        ),
        CheckConstraint(
            "data_type IN ('numeric', 'boolean', 'categorical')",
            name="ck_scores_data_type_known",
        ),
        Index("ix_scores_project_id_trace_id", "project_id", "trace_id"),
        Index("ix_scores_project_id_observation_id", "project_id", "observation_id"),
        Index(
            "ix_scores_project_id_name_scored_at",
            "project_id",
            "name",
            text("scored_at DESC"),
        ),
    )

    # Client-supplied; the idempotency key.
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    project_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    trace_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    observation_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    data_type: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    value_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    # human | llm_judge | heuristic. Unconstrained: an unknown source from a
    # newer SDK is stored, not refused.
    source: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'human'"))
    score_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # Event time, as reported by the SDK.
    scored_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
