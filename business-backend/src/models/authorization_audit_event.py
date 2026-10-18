import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import BaseModel


class AuthorizationAuditEvent(BaseModel):
    __tablename__ = "authorization_audit_events"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    before_state: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    after_state: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        Index(
            "ix_authorization_audit_events_resource",
            "org_id",
            "resource_type",
            "resource_id",
            "created_at",
        ),
        Index(
            "ix_authorization_audit_events_actor",
            "org_id",
            "actor_id",
            "created_at",
        ),
    )
