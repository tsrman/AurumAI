"""A skill Hermes Agent wrote or changed in a user's sandbox, waiting for an
administrator (docs/HERMES_AGENT.md, phase 5b). The organization isolates
rows; the user routes also name the author."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.database import Base

# Register the referenced tables for isolated metadata imports.
from src.models import skill, user  # noqa: F401

STATUSES = ("pending", "approved", "rejected", "dismissed")


class HermesSkillDraft(Base):
    __tablename__ = "hermes_skill_drafts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "user_id"], ["users.org_id", "users.id"], name="fk_hermes_skill_draft_user", ondelete="CASCADE"
        ),
        CheckConstraint(
            "status IN ('pending','approved','rejected','dismissed')", name="ck_hermes_skill_draft_status"
        ),
        CheckConstraint("length(path) BETWEEN 1 AND 200", name="ck_hermes_skill_draft_path"),
        UniqueConstraint("org_id", "user_id", "content_sha256", name="uq_hermes_skill_draft_content"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The skill's directory under Hermes's skills, e.g. `research/arxiv`.
    path: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # Of the file set (paths and bytes); a skill seen again is not drafted again.
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    bundle_storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    bundle_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    # The platform skill an approval created or updated.
    skill_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
