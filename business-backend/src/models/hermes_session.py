"""A Hermes Agent session: the record behind a Hermes session token.

One open session per conversation. Its token is accepted only while the row is
open, unexpired and on the same token version, and only during the turn the
runner bound to it; the turn's workflow correlation is billed from here, never
from the sandbox's requests. Closing clears the binding, so a closed row keeps
no reference to the conversation's turns or runs.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, Text
from sqlalchemy import UniqueConstraint, text as sql_text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import OrgScopedModel

# Register the referenced tables for isolated metadata imports.
from src.models import data_identity, interaction, workflow_run  # noqa: F401


class HermesSession(OrgScopedModel):
    __tablename__ = "hermes_sessions"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_hermes_session_org_id"),
        ForeignKeyConstraint(
            ["org_id", "conversation_id", "user_id"],
            [
                "conversation_identities.org_id",
                "conversation_identities.id",
                "conversation_identities.user_id",
            ],
            name="fk_hermes_session_conversation",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "active_turn_id"],
            ["interaction_turns.org_id", "interaction_turns.id"],
            name="fk_hermes_session_turn",
        ),
        ForeignKeyConstraint(
            ["org_id", "workflow_run_id"],
            ["workflow_runs.org_id", "workflow_runs.id"],
            name="fk_hermes_session_workflow_run",
        ),
        CheckConstraint("state IN ('open','closed')", name="ck_hermes_session_state"),
        CheckConstraint("token_version >= 1", name="ck_hermes_session_token_version"),
        CheckConstraint(
            "(state = 'open' AND closed_at IS NULL) OR (state = 'closed' AND closed_at IS NOT NULL "
            "AND active_turn_id IS NULL AND workflow_run_id IS NULL)",
            name="ck_hermes_session_closed",
        ),
        CheckConstraint(
            "active_turn_id IS NOT NULL OR workflow_run_id IS NULL",
            name="ck_hermes_session_correlation_turn",
        ),
        CheckConstraint(
            "(workflow_run_id IS NULL AND workflow_phase IS NULL AND workflow_node_key IS NULL) OR "
            "(workflow_run_id IS NOT NULL AND ("
            "(workflow_phase = 'node' AND workflow_node_key ~ '^[a-z][a-z0-9_-]{0,63}$') OR "
            "(workflow_phase IN ('supervisor_prepare','supervisor_synthesize') "
            "AND workflow_node_key IS NULL)))",
            name="ck_hermes_session_correlation",
        ),
        Index(
            "uq_hermes_session_open_conversation",
            "org_id",
            "conversation_id",
            unique=True,
            postgresql_where=sql_text("state = 'open'"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(Text, server_default="open", nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    active_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    workflow_phase: Mapped[str | None] = mapped_column(Text)
    workflow_node_key: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
