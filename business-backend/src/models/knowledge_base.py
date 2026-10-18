import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import BaseModel


class KnowledgeBase(BaseModel):
    __tablename__ = "knowledge_bases"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set only for a hidden, auto-provisioned per-conversation knowledge base
    # (chat file attachments) — NULL for every ordinary admin-created one.
    # Cascades with the conversation and is excluded from every normal list
    # query (knowledge_base_repository.list_accessible) so it never appears
    # on the Knowledge Bases page or /admin/access.
    owning_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversation_identities.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
    )

    __table_args__ = (
        CheckConstraint(
            "length(btrim(name)) > 0",
            name="ck_knowledge_bases_name_nonblank",
        ),
        UniqueConstraint("org_id", "name", name="uq_knowledge_bases_org_name"),
        Index("ix_knowledge_bases_org_id_created_at", "org_id", "created_at"),
    )
