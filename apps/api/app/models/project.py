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

    # Keys live in `api_keys` (migration 0006), many per project, each with its
    # own scopes and revocation. The single-key columns that used to sit here
    # were dropped in 0009.

    # The user who created the project through the dashboard (FK to users.id in
    # the database; `users` has no ORM model because the app role only reads
    # it through the auth_* functions). NULL for projects issued from
    # scripts/create_project.py, which are claimed with `--grant`.
    created_by: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )
