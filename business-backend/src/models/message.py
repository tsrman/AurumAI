import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, ForeignKeyConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.models import data_identity  # noqa: F401
from src.models.base import OrgScopedModel


class Message(OrgScopedModel):
    __tablename__ = "messages"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, server_default=func.now())
    __table_args__ = (
        ForeignKeyConstraint(["org_id", "id", "created_at"], ["message_identities.org_id", "message_identities.id", "message_identities.created_at"], ondelete="CASCADE"),
        {"postgresql_partition_by": "RANGE(created_at)"},
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation_identities.id", ondelete="CASCADE"), nullable=False
    )
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    content_ref: Mapped[str | None] = mapped_column(String)
    tool_calls: Mapped[Any] = mapped_column(JSON, default=dict)
    tool_results: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
    model: Mapped[str | None] = mapped_column(String(255))
    tokens_in: Mapped[int | None] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int | None] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), default=0)
    feedback: Mapped[str | None] = mapped_column(String(10))
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
