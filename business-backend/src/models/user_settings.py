import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.core.database import Base


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    theme: Mapped[str | None] = mapped_column(String(20), default="system")
    language: Mapped[str | None] = mapped_column(String(10), default="ru")
    default_model: Mapped[str | None] = mapped_column(String(255))
    code_theme: Mapped[str | None] = mapped_column(String(20), default="github-dark")
    enter_to_send: Mapped[bool | None] = mapped_column(Boolean, default=True)
    show_timestamps: Mapped[bool | None] = mapped_column(Boolean, default=True)
    sidebar_collapsed: Mapped[bool | None] = mapped_column(Boolean, default=False)
    notifications: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, default=lambda: {"email": True, "browser": True}
    )
    shortcuts: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
