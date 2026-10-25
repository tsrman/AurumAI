import uuid

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.core.database import Base


class OrganizationSettings(Base):
    __tablename__ = "organization_settings"
    __table_args__ = (
        CheckConstraint(
            "cardinality(vision_models) <= 100", name="ck_organization_settings_vision_models"
        ),
        CheckConstraint(
            "hermes_egress_mode IS NULL OR hermes_egress_mode IN ('none','packages','public')",
            name="ck_organization_settings_hermes_egress_mode",
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    default_model: Mapped[str | None] = mapped_column(String(255))
    default_temperature: Mapped[float | None] = mapped_column(Float)
    # Image tools use their own models, chosen independently for creating
    # and for editing images.
    image_generation_model: Mapped[str | None] = mapped_column(String(255))
    image_edit_model: Mapped[str | None] = mapped_column(String(255))
    # Chat models the organization marked as accepting image input.
    vision_models: Mapped[list[str]] = mapped_column(
        ARRAY(String(255)), nullable=False, default=list, server_default="{}"
    )
    # Internet a Hermes sandbox may reach; NULL means the default, packages.
    hermes_egress_mode: Mapped[str | None] = mapped_column(Text)
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
