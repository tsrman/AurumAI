"""Durable interaction identity; permissions are deliberately absent from these rows.

Conversation/owner live on the turn; Temporal address lives on the execution.
Composite keys prevent a question/outbox from mixing targets from different turns.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer
from sqlalchemy import String, Text, UniqueConstraint, text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import OrgScopedModel

# Register referenced tables even when a worker/CLI imports this module alone.
from src.models import agent_definition, conversation, user, workflow_run  # noqa: F401


class InteractionTurn(OrgScopedModel):
    __tablename__ = "interaction_turns"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_interaction_turn_org_id"),
        ForeignKeyConstraint(
            ["org_id", "conversation_id", "owner_id"],
            ["conversation_identities.org_id", "conversation_identities.id", "conversation_identities.user_id"],
            name="fk_interaction_turn_conversation",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "owner_id"],
            ["users.org_id", "users.id"],
            name="fk_interaction_turn_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "workflow_run_id"],
            ["workflow_runs.org_id", "workflow_runs.id"],
            name="fk_interaction_turn_workflow_run",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('active','completed','failed','cancelled')",
            name="ck_interaction_turn_status",
        ),
        CheckConstraint("version >= 1", name="ck_interaction_turn_version"),
        CheckConstraint(
            "question_count >= 0 AND search_count >= 0 AND read_count >= 0",
            name="ck_interaction_turn_counters",
        ),
        CheckConstraint("jsonb_typeof(limits) = 'object'", name="ck_interaction_turn_limits"),
        Index(
            "uq_interaction_active_conversation",
            "org_id",
            "conversation_id",
            unique=True,
            postgresql_where=sql_text("status = 'active'"),
        ),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(16), server_default="active", nullable=False)
    version: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    question_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    search_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    read_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    image_count: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InteractionExecution(OrgScopedModel):
    __tablename__ = "interaction_executions"
    __table_args__ = (
        UniqueConstraint("org_id", "turn_id", "id", name="uq_interaction_execution_turn_id"),
        UniqueConstraint(
            "org_id", "turn_id", "workflow_id", "run_id", name="uq_interaction_execution_target"
        ),
        ForeignKeyConstraint(
            ["org_id", "turn_id"],
            ["interaction_turns.org_id", "interaction_turns.id"],
            name="fk_interaction_execution_turn",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "agent_definition_id"],
            ["agent_definitions.org_id", "agent_definitions.id"],
            name="fk_interaction_execution_agent",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(btrim(workflow_id)) > 0 AND length(btrim(task_queue)) > 0",
            name="ck_interaction_execution_address",
        ),
        CheckConstraint(
            "phase IN ('ordinary','node','supervisor_prepare','supervisor_synthesize')",
            name="ck_interaction_execution_phase",
        ),
        CheckConstraint("iteration >= 1", name="ck_interaction_execution_iteration"),
    )

    turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    workflow_id: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    task_queue: Mapped[str] = mapped_column(String(255), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    node_key: Mapped[str | None] = mapped_column(String(64))
    agent_definition_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    iteration: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)


class ToolOperation(OrgScopedModel):
    __tablename__ = "tool_operations"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "turn_id", "execution_id", "id", name="uq_tool_operation_execution_id"
        ),
        UniqueConstraint(
            "org_id", "execution_id", "invocation_id", name="uq_tool_operation_invocation"
        ),
        ForeignKeyConstraint(
            ["org_id", "turn_id", "execution_id"],
            [
                "interaction_executions.org_id",
                "interaction_executions.turn_id",
                "interaction_executions.id",
            ],
            name="fk_tool_operation_execution",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "kind IN ('ask_user','web_search','web_read','image_generate','image_edit')",
            name="ck_tool_operation_kind",
        ),
        CheckConstraint(
            "status IN ('reserved','running','completed','failed','cancelled')",
            name="ck_tool_operation_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_tool_operation_attempts"),
        CheckConstraint(
            "arguments_hash ~ '^[0-9a-f]{64}$'", name="ck_tool_operation_arguments_hash"
        ),
    )

    turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    invocation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), server_default="reserved", nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    lease_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserQuestion(OrgScopedModel):
    __tablename__ = "user_questions"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "turn_id", "execution_id", "id", name="uq_user_question_execution_id"
        ),
        UniqueConstraint("org_id", "operation_id", name="uq_user_question_operation"),
        ForeignKeyConstraint(
            ["org_id", "turn_id", "execution_id", "operation_id"],
            [
                "tool_operations.org_id",
                "tool_operations.turn_id",
                "tool_operations.execution_id",
                "tool_operations.id",
            ],
            name="fk_user_question_operation",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "answered_by"],
            ["users.org_id", "users.id"],
            name="fk_user_question_answerer",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('pending','answered','expired','cancelled')", name="ck_user_question_status"
        ),
        CheckConstraint("version >= 1", name="ck_user_question_version"),
        CheckConstraint(
            "length(text) BETWEEN 1 AND 4000 AND text ~ '[^[:space:]]'",
            name="ck_user_question_text",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR (status = 'answered' AND consumed_at >= answered_at)",
            name="ck_user_question_consumed",
        ),
        CheckConstraint(
            "jsonb_typeof(options) = 'array' AND jsonb_array_length(options) <= 8",
            name="ck_user_question_options",
        ),
        CheckConstraint("expires_at > created_at", name="ck_user_question_deadline"),
        CheckConstraint(
            "(status = 'answered' AND answer IS NOT NULL AND jsonb_typeof(answer) = 'object' "
            "AND answered_at IS NOT NULL AND answered_by IS NOT NULL AND answer_key IS NOT NULL) "
            "OR (status <> 'answered' AND answer IS NULL AND answered_at IS NULL "
            "AND answered_by IS NULL AND answer_key IS NULL)",
            name="ck_user_question_answer_state",
        ),
        Index(
            "uq_question_pending_execution",
            "org_id",
            "execution_id",
            unique=True,
            postgresql_where=sql_text("status = 'pending'"),
        ),
        Index(
            "ix_question_pending_deadline",
            "expires_at",
            postgresql_where=sql_text("status = 'pending'"),
        ),
    )

    turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, server_default=sql_text("'[]'::jsonb"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), server_default="pending", nullable=False)
    version: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    answer: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    answer_key: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InteractionOutbox(OrgScopedModel):
    __tablename__ = "interaction_outbox"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "turn_id", "execution_id", "question_id"],
            [
                "user_questions.org_id",
                "user_questions.turn_id",
                "user_questions.execution_id",
                "user_questions.id",
            ],
            name="fk_interaction_outbox_question",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "org_id",
            "question_id",
            "question_version",
            "event_kind",
            name="uq_interaction_outbox_event",
        ),
        CheckConstraint(
            "event_kind IN ('question_status','question_available')",
            name="ck_interaction_outbox_kind",
        ),
        CheckConstraint(
            "status IN ('pending','delivered','failed','cancelled')",
            name="ck_interaction_outbox_status",
        ),
        CheckConstraint(
            "question_version >= 1 AND attempts >= 0", name="ck_interaction_outbox_counters"
        ),
        Index(
            "ix_interaction_outbox_pending",
            "available_at",
            postgresql_where=sql_text("status = 'pending'"),
        ),
    )

    turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    question_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    question_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), server_default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("now()"), nullable=False
    )
    lease_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
