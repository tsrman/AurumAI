import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.models.base import BaseModel


class Group(BaseModel):
    __tablename__ = "groups"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    keycloak_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True, index=True)
    # Set (and refreshed) each time Keycloak's `groups` claim materializes this
    # group during sync_keycloak_user — NULL means the group was only ever
    # created in-platform (pre-provisioned) and never matched by a real login.
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
