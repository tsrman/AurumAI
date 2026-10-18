import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.core.database import Base


class OrganizationSettings(Base):
    __tablename__ = "organization_settings"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    default_model: Mapped[str | None] = mapped_column(String(255))
    default_temperature: Mapped[float | None] = mapped_column(Float)
    allowed_models: Mapped[list[str] | None] = mapped_column(
        ARRAY(String), default=lambda: ["gpt-4o", "claude-3-5-sonnet"]
    )
    max_upload_size_mb: Mapped[int | None] = mapped_column(Integer, default=10)
    allowed_file_types: Mapped[list[str] | None] = mapped_column(
        ARRAY(String), default=lambda: ["pdf", "png", "jpg", "docx", "txt"]
    )
    retention_days: Mapped[int | None] = mapped_column(Integer, default=365)
    require_approval_for_models: Mapped[list[str] | None] = mapped_column(ARRAY(String), default=list)
    custom_system_prompt: Mapped[str | None] = mapped_column(Text)
    moderation_enabled: Mapped[bool | None] = mapped_column(Boolean, default=True)
    watermark_enabled: Mapped[bool | None] = mapped_column(Boolean, default=False)
