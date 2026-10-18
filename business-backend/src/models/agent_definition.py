import uuid

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import BaseModel


class AgentDefinition(BaseModel):
    __tablename__ = "agent_definitions"

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
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL means "inherit the organization default" (resolved server-side,
    # see services/model_parameters.py::resolve_temperature) — kept as a
    # sensible default at creation time via server_default, but explicitly
    # nullable so an update can clear the override. evaluates_none preserves
    # explicit None on INSERT as SQL NULL instead of applying the default.
    temperature: Mapped[float | None] = mapped_column(
        Float().evaluates_none(), nullable=True, default=0.3, server_default="0.3"
    )
    skill_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list, server_default="{}"
    )
    knowledge_base_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, default=list, server_default="{}"
    )

    __table_args__ = (
        CheckConstraint(
            "length(btrim(name)) > 0",
            name="ck_agent_definitions_name_nonblank",
        ),
        CheckConstraint(
            "length(btrim(model_id)) > 0",
            name="ck_agent_definitions_model_nonblank",
        ),
        CheckConstraint(
            "length(btrim(system_prompt)) > 0",
            name="ck_agent_definitions_prompt_nonblank",
        ),
        CheckConstraint(
            "temperature >= 0 AND temperature <= 2",
            name="ck_agent_definitions_temperature",
        ),
        UniqueConstraint("org_id", "name", name="uq_agent_definitions_org_name"),
        UniqueConstraint("org_id", "id", name="uq_agent_definitions_org_id_id"),
        Index("ix_agent_definitions_org_id_created_at", "org_id", "created_at"),
    )
