import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.models.base import BaseModel


class Skill(BaseModel):
    __tablename__ = "skills"

    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    # Set only for a hidden, auto-provisioned per-conversation
    # execute_hybrid_search skill (chat file attachments) — NULL for every
    # ordinary admin-created skill. See KnowledgeBase.owning_conversation_id
    # for the matching column on the knowledge base it searches.
    owning_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversation_identities.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    workflow_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    activity_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mcp_transport: Mapped[str | None] = mapped_column(String(50), nullable=True)
    mcp_endpoint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    mcp_auth_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
    allowed_child_skills: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    bundle_storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    bundle_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bundle_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bundle_skill_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    network_policy: Mapped[str] = mapped_column(String(20), nullable=False, default="none", server_default="none")

    __table_args__ = (
        Index("ix_skills_org_id_type", "org_id", "type"),
    )
