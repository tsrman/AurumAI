"""Narrow, durable lifecycle fences. No workflow inputs or external payloads."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import OrgScopedModel


class ConversationOperation(OrgScopedModel):
    __tablename__ = "conversation_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "conversation_id"],
            ["conversation_identities.org_id", "conversation_identities.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "conversation_id", "generation", "kind", "operation_key"),
        CheckConstraint("status IN ('pending','settled')", name="ck_conversation_operation_status"),
        CheckConstraint("generation > 0", name="ck_conversation_operation_generation"),
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    generation: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(32))
    operation_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    workflow_id: Mapped[str | None] = mapped_column(String(255))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    resource_key: Mapped[str | None] = mapped_column(String(1024))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationExecution(OrgScopedModel):
    __tablename__ = "conversation_executions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "conversation_id"],
            ["conversation_identities.org_id", "conversation_identities.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("namespace", "workflow_id", "run_id"),
        CheckConstraint("generation > 0", name="ck_conversation_execution_generation"),
        CheckConstraint(
            "NOT tree_complete OR (terminal_status IS NOT NULL AND closed_at IS NOT NULL AND observed_at IS NOT NULL)",
            name="ck_conversation_execution_receipt",
        ),
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    generation: Mapped[int] = mapped_column(BigInteger)
    namespace: Mapped[str] = mapped_column(String(255))
    workflow_id: Mapped[str] = mapped_column(String(255))
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    workflow_type: Mapped[str] = mapped_column(String(64))
    task_queue: Mapped[str] = mapped_column(String(255))
    parent_workflow_id: Mapped[str | None] = mapped_column(String(255))
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    initiated_event_id: Mapped[int | None] = mapped_column(BigInteger)
    terminal_status: Mapped[str | None] = mapped_column(String(32))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tree_complete: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    blocked_reason: Mapped[str | None] = mapped_column(String(64))
