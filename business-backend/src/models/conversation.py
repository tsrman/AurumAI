import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, JSON, Numeric, String, Text, ForeignKeyConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.models import data_identity  # noqa: F401
from src.models.base import OrgScopedModel


class Conversation(OrgScopedModel):
    __tablename__ = "conversations"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, server_default=func.now())
    __table_args__ = (
        ForeignKeyConstraint(["org_id", "id", "created_at"], ["conversation_identities.org_id", "conversation_identities.id", "conversation_identities.created_at"], ondelete="CASCADE"),
        {"postgresql_partition_by": "RANGE(created_at)"},
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(500))
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    mode: Mapped[str | None] = mapped_column(String(20), default="chat")
    system_prompt: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(20), default="active")
    temperature: Mapped[float | None] = mapped_column(Float)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
    token_count_in: Mapped[int | None] = mapped_column(BigInteger, default=0)
    token_count_out: Mapped[int | None] = mapped_column(BigInteger, default=0)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def agent_definition_id(self) -> uuid.UUID | None:
        """The administrator's agent the conversation last ran on, so the composer keeps it
        chosen. Kept in `extra` (no new column, so the archive format is untouched); a stale id
        is harmless, the agent is resolved afresh on every turn."""
        raw = (self.extra or {}).get("agent_definition_id")
        try:
            return uuid.UUID(str(raw)) if raw else None
        except ValueError:
            return None

    @agent_definition_id.setter
    def agent_definition_id(self, value: uuid.UUID | None) -> None:
        extra = dict(self.extra or {})
        if value is None:
            extra.pop("agent_definition_id", None)
        else:
            extra["agent_definition_id"] = str(value)
        self.extra = extra
