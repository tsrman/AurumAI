import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKeyConstraint, Integer, String
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.models import data_identity  # noqa: F401

from src.models.base import OrgScopedModel


class AuditLog(OrgScopedModel):
    """Record who changed what through mutating requests.

    Request bodies are intentionally excluded: these routes also carry MCP
    tokens, S3 keys and passwords, and the journal must not store another copy.
    """

    __tablename__ = "audit_logs"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, server_default=func.now())
    __table_args__ = (ForeignKeyConstraint(["org_id", "id", "created_at"], ["audit_log_identities.org_id", "audit_log_identities.id", "audit_log_identities.created_at"], ondelete="CASCADE"), {"postgresql_partition_by": "RANGE(created_at)"})

    # Keep action history after a user is deleted, so user_id is deliberately
    # not a foreign key.
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    # Store a route template (/v1/skills/{skill_id}) rather than an object URL
    # so rows group by operation rather than resource.
    path: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    ip_address: Mapped[Optional[str]] = mapped_column(INET, nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
