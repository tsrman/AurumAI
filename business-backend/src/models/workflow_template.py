import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import BaseModel


class WorkflowTemplateStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class WorkflowTemplate(BaseModel):
    __tablename__ = "workflow_templates"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=WorkflowTemplateStatus.ACTIVE.value,
        server_default=text("'active'"),
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "length(btrim(name)) > 0",
            name="ck_workflow_templates_name_nonblank",
        ),
        CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_workflow_templates_status",
        ),
        CheckConstraint("version >= 1", name="ck_workflow_templates_version"),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_workflow_templates_snapshot_object",
        ),
        UniqueConstraint("org_id", "name", name="uq_workflow_templates_org_name"),
        UniqueConstraint("org_id", "id", name="uq_workflow_templates_org_id_id"),
        Index(
            "ix_workflow_templates_org_status_name",
            "org_id",
            "status",
            "name",
        ),
    )
