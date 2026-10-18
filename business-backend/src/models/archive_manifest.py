"""Permanent narrow archive control, never source payload."""

from datetime import datetime
from typing import Any
from uuid import UUID
from sqlalchemy import DateTime, ForeignKeyConstraint, String, Integer
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from src.models.base import OrgScopedModel


class ArchiveJob(OrgScopedModel):
    __tablename__ = "archive_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "conversation_id"],
            ["conversation_identities.org_id", "conversation_identities.id"],
            ondelete="RESTRICT",
        ),
    )
    kind: Mapped[str] = mapped_column(String(16))
    conversation_id: Mapped[UUID | None] = mapped_column(PGUUID)
    generation: Mapped[int] = mapped_column(Integer)
    cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(24), default="claimed")
    inventory: Mapped[dict[str, Any]] = mapped_column(JSONB)
    manifest_hash: Mapped[str | None] = mapped_column(String(64))
    rag: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    restore_generation: Mapped[int | None] = mapped_column(Integer)
    staged: Mapped[bool] = mapped_column(default=False)
