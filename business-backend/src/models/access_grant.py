import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import BaseModel


class AccessGrant(BaseModel):
    __tablename__ = "access_grants"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    permission: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_via: Mapped[str] = mapped_column(
        String(20), nullable=False, default="admin", server_default="'admin'"
    )

    __table_args__ = (
        CheckConstraint(
            "resource_type IN "
            "('model', 'skill', 'knowledge_base', 'agent_definition', 'workflow')",
            name="ck_access_grants_resource_type",
        ),
        CheckConstraint(
            "principal_type IN ('organization', 'group', 'user')",
            name="ck_access_grants_principal_type",
        ),
        CheckConstraint(
            "permission IN ('read', 'write')",
            name="ck_access_grants_permission",
        ),
        CheckConstraint(
            "created_via IN ('admin', 'migration', 'provisioning')",
            name="ck_access_grants_created_via",
        ),
        CheckConstraint(
            "((created_via = 'admin' AND created_by IS NOT NULL) OR "
            "(created_via IN ('migration', 'provisioning') AND created_by IS NULL))",
            name="ck_access_grants_source_actor",
        ),
        UniqueConstraint(
            "org_id",
            "resource_type",
            "resource_id",
            "principal_type",
            "principal_id",
            "permission",
            name="uq_access_grants_resource_principal_permission",
        ),
        Index(
            "ix_access_grants_resource_lookup",
            "org_id",
            "resource_type",
            "resource_id",
            "permission",
        ),
        Index(
            "ix_access_grants_principal_lookup",
            "org_id",
            "principal_type",
            "principal_id",
            "permission",
        ),
    )
