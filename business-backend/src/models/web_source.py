"""Immutable web-source provenance with separately scrubbed normalized content."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, Integer, Text
from sqlalchemy import UniqueConstraint, text as sql_text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import OrgScopedModel

# Register the referenced interaction tables for isolated metadata imports.
from src.models import interaction  # noqa: F401


class WebSource(OrgScopedModel):
    __tablename__ = "web_sources"
    __table_args__ = (
        UniqueConstraint("org_id", "turn_id", "id", name="uq_web_source_turn_id"),
        UniqueConstraint("org_id", "operation_id", "position", name="uq_web_source_position"),
        ForeignKeyConstraint(
            ["org_id", "turn_id", "execution_id", "operation_id"],
            [
                "tool_operations.org_id",
                "tool_operations.turn_id",
                "tool_operations.execution_id",
                "tool_operations.id",
            ],
            name="fk_web_source_operation",
            ondelete="CASCADE",
        ),
        CheckConstraint("kind IN ('search','read')", name="ck_web_source_kind"),
        CheckConstraint("position BETWEEN 0 AND 4", name="ck_web_source_position"),
        CheckConstraint(
            "length(final_url) BETWEEN 1 AND 8192", name="ck_web_source_final_url"
        ),
        CheckConstraint(
            "requested_url IS NULL OR length(requested_url) BETWEEN 1 AND 8192",
            name="ck_web_source_requested_url",
        ),
        CheckConstraint(
            "title IS NULL OR length(title) <= 2000", name="ck_web_source_title"
        ),
        CheckConstraint(
            "content IS NULL OR length(content) <= 50000", name="ck_web_source_content"
        ),
        CheckConstraint(
            "page_count IS NULL OR page_count >= 0", name="ck_web_source_page_count"
        ),
    )

    turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    requested_url: Mapped[str | None] = mapped_column(Text)
    final_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("clock_timestamp()"), nullable=False
    )
    truncated: Mapped[bool] = mapped_column(Boolean, server_default="false", nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
