import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import BaseModel


class WorkflowRunStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowRun(BaseModel):
    __tablename__ = "workflow_runs"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    workflow_definition_version: Mapped[int] = mapped_column(Integer, nullable=False)
    initiating_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    temporal_workflow_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    execution_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=WorkflowRunStatus.PENDING.value,
        server_default=text("'pending'"),
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    internal_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "id", "created_at"],
            ["workflow_run_identities.org_id", "workflow_run_identities.id", "workflow_run_identities.created_at"],
            name="fk_payload_identity_workflow_runs", ondelete="CASCADE",
        ),
        CheckConstraint(
            "workflow_definition_version >= 1",
            name="ck_workflow_runs_definition_version",
        ),
        CheckConstraint(
            "status IN ('pending', 'planning', 'running', 'synthesizing', "
            "'completed', 'failed', 'cancelled')",
            name="ck_workflow_runs_status",
        ),
        CheckConstraint(
            "jsonb_typeof(execution_snapshot) = 'object'",
            name="ck_workflow_runs_snapshot_object",
        ),
        UniqueConstraint("org_id", "id", name="uq_workflow_runs_org_id_id"),
        UniqueConstraint(
            "conversation_id", "turn_id", name="uq_workflow_runs_conversation_turn"
        ),
        ForeignKeyConstraint(
            ["org_id", "workflow_definition_id"],
            ["workflow_definitions.org_id", "workflow_definitions.id"],
            name="fk_workflow_runs_org_definition",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "initiating_user_id"],
            ["users.org_id", "users.id"],
            name="fk_workflow_runs_org_user",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "conversation_id"],
            ["conversation_identities.org_id", "conversation_identities.id"],
            name="fk_workflow_runs_org_conversation",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "source_message_id"],
            ["message_identities.org_id", "message_identities.id"],
            name="fk_workflow_runs_org_message",
            ondelete="CASCADE",
        ),
        Index("ix_workflow_runs_org_user_created", "org_id", "initiating_user_id", "created_at"),
        Index("ix_workflow_runs_org_status_created", "org_id", "status", "created_at"),
    )


class WorkflowNodeRun(BaseModel):
    __tablename__ = "workflow_node_runs"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    phase_key: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default=text("'pending'")
    )
    attempt_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    internal_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    diagnostic_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'waiting_human', 'completed', "
            "'failed', 'skipped', 'cancelled')",
            name="ck_workflow_node_runs_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_workflow_node_runs_attempt_count",
        ),
        CheckConstraint(
            "length(btrim(phase_key)) > 0",
            name="ck_workflow_node_runs_phase_key_nonblank",
        ),
        CheckConstraint(
            "length(btrim(agent_name)) > 0",
            name="ck_workflow_node_runs_agent_name_nonblank",
        ),
        UniqueConstraint(
            "workflow_run_id", "phase_key", name="uq_workflow_node_runs_run_phase"
        ),
        ForeignKeyConstraint(
            ["org_id", "workflow_run_id"],
            ["workflow_runs.org_id", "workflow_runs.id"],
            name="fk_workflow_node_runs_org_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "agent_definition_id"],
            ["agent_definitions.org_id", "agent_definitions.id"],
            name="fk_workflow_node_runs_org_agent",
            ondelete="RESTRICT",
        ),
        Index("ix_workflow_node_runs_org_run", "org_id", "workflow_run_id"),
        Index("ix_workflow_node_runs_diagnostic_expiry", "diagnostic_expires_at"),
    )
