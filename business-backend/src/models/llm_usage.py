import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.core.database import Base


class UsageEventStatus(StrEnum):
    RESERVED = "reserved"
    ACCEPTED = "accepted"
    COMPLETED = "completed"


class LLMUsageEvent(Base):
    __tablename__ = "llm_usage_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('reserved', 'accepted', 'completed')",
            name="ck_llm_usage_events_status",
        ),
        CheckConstraint("prompt_tokens >= 0", name="ck_llm_usage_events_prompt_tokens"),
        CheckConstraint(
            "completion_tokens >= 0",
            name="ck_llm_usage_events_completion_tokens",
        ),
        CheckConstraint("total_tokens >= 0", name="ck_llm_usage_events_total_tokens"),
        CheckConstraint(
            "(workflow_run_id IS NULL AND workflow_phase IS NULL "
            "AND workflow_node_key IS NULL) OR "
            "(workflow_run_id IS NOT NULL AND "
            "((workflow_phase = 'node' AND workflow_node_key IS NOT NULL) OR "
            "(workflow_phase IN ('supervisor_prepare', 'supervisor_synthesize') "
            "AND workflow_node_key IS NULL)))",
            name="ck_llm_usage_events_workflow_correlation",
        ),
        ForeignKeyConstraint(
            ["org_id", "workflow_run_id"],
            ["workflow_run_identities.org_id", "workflow_run_identities.id"],
            name="fk_llm_usage_events_workflow_run",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    workflow_phase: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workflow_node_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=UsageEventStatus.RESERVED.value,
        server_default="'reserved'",
    )
    prompt_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    total_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrganizationUsageMonthly(Base):
    __tablename__ = "organization_usage_monthly"
    __table_args__ = (
        CheckConstraint(
            "requests_used >= 0", name="ck_organization_usage_monthly_requests_used"
        ),
        CheckConstraint(
            "prompt_tokens >= 0", name="ck_organization_usage_monthly_prompt_tokens"
        ),
        CheckConstraint(
            "completion_tokens >= 0",
            name="ck_organization_usage_monthly_completion_tokens",
        ),
        CheckConstraint(
            "total_tokens >= 0", name="ck_organization_usage_monthly_total_tokens"
        ),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    period_start: Mapped[date] = mapped_column(Date, primary_key=True)
    requests_used: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    prompt_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    total_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
