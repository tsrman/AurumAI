"""Permanent UUID/natural keys; payload and lifecycle have separate privileges."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, BigInteger, DateTime, ForeignKeyConstraint, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class DataIdentity(Base):
    __abstract__ = True
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    lifecycle: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    lease_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archive_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class ConversationIdentity(DataIdentity):
    __tablename__ = "conversation_identities"
    execution_provenance_complete: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("org_id", "id", "created_at"),
        UniqueConstraint("org_id", "id", "user_id"),
        ForeignKeyConstraint(
            ["org_id", "user_id"], ["users.org_id", "users.id"], ondelete="CASCADE"
        ),
    )


class MessageIdentity(DataIdentity):
    __tablename__ = "message_identities"
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("org_id", "id", "created_at"),
        UniqueConstraint("conversation_id", "turn_id", "role"),
        ForeignKeyConstraint(
            ["org_id", "conversation_id"],
            ["conversation_identities.org_id", "conversation_identities.id"],
            ondelete="CASCADE",
        ),
    )


class WorkflowRunIdentity(DataIdentity):
    __tablename__ = "workflow_run_identities"
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    initiating_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    temporal_workflow_id: Mapped[str] = mapped_column(String(255), nullable=False)
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("org_id", "id", "created_at"),
        UniqueConstraint("conversation_id", "turn_id"),
        UniqueConstraint("temporal_workflow_id"),
        ForeignKeyConstraint(
            ["org_id", "conversation_id"],
            ["conversation_identities.org_id", "conversation_identities.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "source_message_id"],
            ["message_identities.org_id", "message_identities.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "initiating_user_id"],
            ["users.org_id", "users.id"],
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
    )


class AuditLogIdentity(DataIdentity):
    __tablename__ = "audit_log_identities"
    __table_args__ = (
        UniqueConstraint("org_id", "id"),
        UniqueConstraint("org_id", "id", "created_at"),
    )
