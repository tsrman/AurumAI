import uuid

from sqlalchemy import CheckConstraint, ForeignKey, PrimaryKeyConstraint, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.core.database import Base


class UserGroup(Base):
    __tablename__ = "user_groups"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # "identity": owned by sign-in synchronization with the Keycloak `groups`
    # claim. "manual": added by an administrator; synchronization never touches it.
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="identity", server_default="identity"
    )

    __table_args__ = (
        PrimaryKeyConstraint("user_id", "group_id"),
        CheckConstraint("source IN ('identity', 'manual')", name="ck_user_groups_source"),
    )
