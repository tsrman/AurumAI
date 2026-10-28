"""Hermes Agent's memory of one user (docs/HERMES_AGENT.md, phase 5a): its notes
(`memory`, MEMORY.md) and its profile of the user (`user`, USER.md). The
organization isolates rows; every read and write also names the owning user."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.database import Base

# Register the referenced table for isolated metadata imports.
from src.models import user  # noqa: F401


class HermesMemory(Base):
    __tablename__ = "hermes_memories"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "user_id"], ["users.org_id", "users.id"], name="fk_hermes_memory_user", ondelete="CASCADE"
        ),
        CheckConstraint("kind IN ('memory','user')", name="ck_hermes_memory_kind"),
        CheckConstraint(
            "(kind = 'memory' AND length(content) <= 2200) OR (kind = 'user' AND length(content) <= 1375)",
            name="ck_hermes_memory_size",
        ),
        CheckConstraint("version >= 1", name="ck_hermes_memory_version"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
