import uuid
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import BaseModel
from src.models import workflow_template  # noqa: F401


class WorkflowDefinitionStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class WorkflowDefinition(BaseModel):
    __tablename__ = "workflow_definitions"

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
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    supervisor_agent_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    max_parallelism: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=4, server_default=text("4")
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=WorkflowDefinitionStatus.ACTIVE.value,
        server_default=text("'active'"),
    )
    # Opt-in gate for automatic routing (ROADMAP C.3): chat_stream() only ever
    # runs the auto-routing classifier when the org has at least one
    # accessible definition with this set, so an org that never opts in pays
    # zero extra LLM calls per turn — see business-backend/src/routers/chat.py.
    auto_routable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    source_template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    source_template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "length(btrim(name)) > 0",
            name="ck_workflow_definitions_name_nonblank",
        ),
        CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_workflow_definitions_status",
        ),
        CheckConstraint("version >= 1", name="ck_workflow_definitions_version"),
        CheckConstraint(
            "max_parallelism >= 1 AND max_parallelism <= 4",
            name="ck_workflow_definitions_parallelism",
        ),
        UniqueConstraint(
            "org_id", "name", name="uq_workflow_definitions_org_name"
        ),
        UniqueConstraint(
            "org_id", "id", name="uq_workflow_definitions_org_id_id"
        ),
        ForeignKeyConstraint(
            ["org_id", "supervisor_agent_definition_id"],
            ["agent_definitions.org_id", "agent_definitions.id"],
            name="fk_workflow_definitions_org_supervisor",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "source_template_id"],
            ["workflow_templates.org_id", "workflow_templates.id"],
            name="fk_workflow_definitions_org_source_template",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(source_template_id IS NULL AND source_template_version IS NULL) "
            "OR (source_template_id IS NOT NULL "
            "AND source_template_version IS NOT NULL "
            "AND source_template_version >= 1)",
            name="ck_workflow_definitions_source_template_provenance",
        ),
        Index(
            "ix_workflow_definitions_org_status_name",
            "org_id",
            "status",
            "name",
        ),
    )


class WorkflowNode(BaseModel):
    __tablename__ = "workflow_nodes"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    node_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    position_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_y: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Captured into the run's immutable execution_snapshot at run-creation
    # time — editing this later only affects future runs, same as every
    # other node property (agent, instruction, ...).
    requires_confirmation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    __table_args__ = (
        CheckConstraint(
            "node_key ~ '^[a-z][a-z0-9_-]{0,63}$'",
            name="ck_workflow_nodes_key",
        ),
        CheckConstraint(
            "length(btrim(display_name)) > 0",
            name="ck_workflow_nodes_display_name_nonblank",
        ),
        CheckConstraint(
            "length(btrim(instruction)) > 0",
            name="ck_workflow_nodes_instruction_nonblank",
        ),
        UniqueConstraint(
            "workflow_definition_id",
            "node_key",
            name="uq_workflow_nodes_definition_key",
        ),
        UniqueConstraint(
            "org_id",
            "workflow_definition_id",
            "id",
            name="uq_workflow_nodes_org_definition_id",
        ),
        ForeignKeyConstraint(
            ["org_id", "workflow_definition_id"],
            ["workflow_definitions.org_id", "workflow_definitions.id"],
            name="fk_workflow_nodes_org_definition",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "agent_definition_id"],
            ["agent_definitions.org_id", "agent_definitions.id"],
            name="fk_workflow_nodes_org_agent",
            ondelete="RESTRICT",
        ),
        Index("ix_workflow_nodes_org_definition", "org_id", "workflow_definition_id"),
    )


class WorkflowEdge(BaseModel):
    __tablename__ = "workflow_edges"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    workflow_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    source_node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Captured into the run's immutable execution_snapshot at run-creation
    # time, same as every WorkflowNode property. AND-join scheduling: a
    # target becomes eligible only once every incoming edge's condition is
    # satisfied by its source's terminal outcome.
    condition: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="on_success",
        server_default=text("'on_success'"),
    )
    # A loop-back edge is the one deliberate exception to the DAG's
    # acyclic-only rule — see services/workflow_graph.py for cycle/body
    # validation. max_iterations is required exactly when is_loop_back is
    # true. Time-based caps are deferred (see
    # docs/ARCHITECTURE.md).
    is_loop_back: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    max_iterations: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "is_loop_back OR source_node_id <> target_node_id",
            name="ck_workflow_edges_no_self_edge",
        ),
        CheckConstraint(
            "condition IN ('on_success', 'on_failure')",
            name="ck_workflow_edges_condition",
        ),
        CheckConstraint(
            "(is_loop_back AND max_iterations BETWEEN 2 AND 20) "
            "OR (NOT is_loop_back AND max_iterations IS NULL)",
            name="ck_workflow_edges_max_iterations",
        ),
        UniqueConstraint(
            "workflow_definition_id",
            "source_node_id",
            "target_node_id",
            name="uq_workflow_edges_definition_pair",
        ),
        ForeignKeyConstraint(
            ["org_id", "workflow_definition_id"],
            ["workflow_definitions.org_id", "workflow_definitions.id"],
            name="fk_workflow_edges_org_definition",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "workflow_definition_id", "source_node_id"],
            ["workflow_nodes.org_id", "workflow_nodes.workflow_definition_id", "workflow_nodes.id"],
            name="fk_workflow_edges_source_node",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "workflow_definition_id", "target_node_id"],
            ["workflow_nodes.org_id", "workflow_nodes.workflow_definition_id", "workflow_nodes.id"],
            name="fk_workflow_edges_target_node",
            ondelete="CASCADE",
        ),
        Index("ix_workflow_edges_org_definition", "org_id", "workflow_definition_id"),
    )
