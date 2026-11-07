import uuid
from datetime import datetime

from sqlalchemy import DateTime, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.database import Base


class BaseModel(Base):
    __abstract__ = True

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class OrgScopedModel(BaseModel):
    __abstract__ = True

    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)


class OrgScopedLeadingIndexModel(BaseModel):
    """Like OrgScopedModel, for tables whose own composite indexes already lead with org_id.

    A separate index on org_id alone would repeat the start of those and only add to the cost
    of every write on a table that is written a lot.
    """

    __abstract__ = True

    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
