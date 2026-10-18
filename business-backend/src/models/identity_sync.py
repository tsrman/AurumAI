"""Private, bounded identity snapshots; deliberately no FK to unprovisioned users."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.database import Base


class IdentitySyncState(Base):
    __tablename__ = "identity_sync_state"

    org_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    requested_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    applied_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    security_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    claims_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_iat: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    last_requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdentitySyncInbox(Base):
    __tablename__ = "identity_sync_inbox"

    org_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_category: Mapped[str | None] = mapped_column(String(32))
    next_dispatch_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
