"""Previews of the apps Hermes Agent runs in its sandbox (docs/HERMES_AGENT.md).

A preview names a port of a session's sandbox, shared with the conversation's
owner for the turn that shared it. A ticket is the one-time pass the owner's
browser exchanges, at the preview's own origin, for that origin's cookie.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import OrgScopedModel

# Register the referenced tables for isolated metadata imports.
from src.models import hermes_session, interaction  # noqa: F401


class HermesPreview(OrgScopedModel):
    __tablename__ = "hermes_previews"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_hermes_preview_org_id"),
        UniqueConstraint("org_id", "session_id", "port", name="uq_hermes_preview_session_port"),
        ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["hermes_sessions.org_id", "hermes_sessions.id"],
            name="fk_hermes_preview_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "turn_id"],
            ["interaction_turns.org_id", "interaction_turns.id"],
            name="fk_hermes_preview_turn",
            ondelete="CASCADE",
        ),
        CheckConstraint("port BETWEEN 1024 AND 65535", name="ck_hermes_preview_port"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)


class HermesPreviewTicket(OrgScopedModel):
    __tablename__ = "hermes_preview_tickets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "preview_id"],
            ["hermes_previews.org_id", "hermes_previews.id"],
            name="fk_hermes_preview_ticket_preview",
            ondelete="CASCADE",
        ),
    )

    preview_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
