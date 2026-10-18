import uuid

from sqlalchemy import CheckConstraint, ForeignKey, PrimaryKeyConstraint, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.core.capability_catalog import CAPABILITY_VALUES_SQL
from src.core.database import Base


class OrganizationCapability(Base):
    __tablename__ = "organization_capabilities"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    capability: Mapped[str] = mapped_column(String(100), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("org_id", "capability"),
        CheckConstraint(
            f"capability IN ({CAPABILITY_VALUES_SQL})",
            name="ck_organization_capabilities_capability",
        ),
    )
