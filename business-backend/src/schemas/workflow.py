from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from src.models.workflow_definition import WorkflowDefinitionStatus
from src.models.workflow_run import WorkflowRunStatus


class WorkflowSchema(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        from_attributes=True,
    )


# workflow_node_runs stores the two supervisor phases and every task node in
# one table keyed by (workflow_run_id, phase_key), so a node whose key collides
# with a supervisor phase breaks the unique constraint at run creation — an
# IntegrityError on /v1/chat/stream that no handler there catches, on every run
# of an otherwise valid definition.  Reject the collision at authoring time.
RESERVED_NODE_KEYS = frozenset({"supervisor_prepare", "supervisor_synthesize"})


class WorkflowNodeDraft(WorkflowSchema):
    node_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    display_name: str = Field(min_length=1, max_length=255)
    agent_definition_id: UUID
    instruction: str = Field(min_length=1, max_length=20_000)
    position_x: float | None = None
    position_y: float | None = None
    requires_confirmation: bool = False

    @field_validator("display_name", "instruction")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("node_key")
    @classmethod
    def reject_reserved_node_key(cls, value: str) -> str:
        if value in RESERVED_NODE_KEYS:
            raise ValueError(
                f"node key {value!r} is reserved for a supervisor phase"
            )
        return value


class WorkflowEdgeDraft(WorkflowSchema):
    source_node_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    target_node_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    condition: Literal["on_success", "on_failure"] = "on_success"
    # A loop-back edge is the one deliberate exception to the DAG's
    # acyclic-only rule (enforced in services/workflow_graph.py, not here —
    # cycle detection needs the whole edge list). max_iterations bounds how
    # many times the loop body may re-run. Time-based caps are deferred (see
    # docs/ARCHITECTURE.md) — forbidden
    # on an ordinary edge so a stray value can't silently do nothing.
    is_loop_back: bool = False
    max_iterations: int | None = Field(default=None, ge=2, le=20)

    @model_validator(mode="after")
    def validate_loop_fields(self) -> "WorkflowEdgeDraft":
        if self.is_loop_back and self.max_iterations is None:
            raise ValueError("maxIterations is required when isLoopBack is true")
        if not self.is_loop_back and self.max_iterations is not None:
            raise ValueError("maxIterations is only valid when isLoopBack is true")
        return self


class WorkflowDefinitionDraft(WorkflowSchema):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)
    supervisor_agent_definition_id: UUID
    max_parallelism: int = Field(default=4, ge=1, le=4)
    auto_routable: bool = False
    nodes: list[WorkflowNodeDraft] = Field(min_length=1, max_length=20)
    edges: list[WorkflowEdgeDraft] = Field(max_length=380)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def reject_duplicate_graph_identifiers(self) -> "WorkflowDefinitionDraft":
        node_keys = [node.node_key for node in self.nodes]
        if len(node_keys) != len(set(node_keys)):
            raise ValueError("node keys must be unique")
        edge_keys = [
            (edge.source_node_key, edge.target_node_key) for edge in self.edges
        ]
        if len(edge_keys) != len(set(edge_keys)):
            raise ValueError("edges must be unique")
        return self


class WorkflowDefinitionCreate(WorkflowDefinitionDraft):
    pass


class WorkflowDefinitionReplace(WorkflowDefinitionDraft):
    version: int = Field(ge=1)


class WorkflowDefinitionResponse(WorkflowDefinitionDraft):
    id: UUID
    org_id: UUID
    version: int = Field(ge=1)
    status: WorkflowDefinitionStatus
    created_by: UUID | None
    updated_by: UUID | None
    created_at: datetime
    updated_at: datetime
    source_template_id: UUID | None = None
    source_template_version: int | None = Field(default=None, ge=1)


class WorkflowListItem(WorkflowSchema):
    id: UUID
    name: str
    description: str | None
    version: int = Field(ge=1)
    max_parallelism: int = Field(ge=1, le=4)
    status: WorkflowDefinitionStatus
    auto_routable: bool = False


class WorkflowLoopBody(WorkflowSchema):
    """The node set a loop-back edge governs, precomputed once by
    services/workflow_graph.py's validator (descendants(target) ∩
    ancestors(source) over structural edges, inclusive) so neither the API
    response nor the Temporal scheduler recomputes graph traversal — both
    read this instead, guaranteeing they agree on what "the loop body" is.
    """

    source_node_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    target_node_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    condition: Literal["on_success", "on_failure"]
    max_iterations: int = Field(ge=2, le=20)
    node_keys: tuple[str, ...]


class WorkflowValidationResponse(WorkflowSchema):
    valid: bool = True
    topological_order: tuple[str, ...]
    predecessors: dict[str, tuple[str, ...]]
    loop_bodies: tuple[WorkflowLoopBody, ...] = ()


class WorkflowKnowledgeBaseSnapshot(WorkflowSchema):
    id: UUID
    name: str = Field(min_length=1, max_length=255)


class WorkflowAgentSnapshot(WorkflowSchema):
    agent_definition_id: UUID
    name: str = Field(min_length=1, max_length=255)
    model: str = Field(min_length=1, max_length=255)
    system_prompt: str = Field(min_length=1, max_length=50_000)
    temperature: float = Field(ge=0, le=2)
    allowed_skill_ids: tuple[UUID, ...] = Field(max_length=100)
    knowledge_bases: tuple[WorkflowKnowledgeBaseSnapshot, ...] = Field(max_length=100)
    # "hermes": the node runs on Hermes Agent, on the organization's Hermes
    # model at the run's start.
    kind: Literal["agent", "hermes"] = "agent"


class WorkflowSnapshotNode(WorkflowSchema):
    node_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    display_name: str = Field(min_length=1, max_length=255)
    instruction: str = Field(min_length=1, max_length=20_000)
    agent: WorkflowAgentSnapshot
    requires_confirmation: bool = False


class WorkflowRunSnapshotResponse(WorkflowSchema):
    run_id: UUID
    org_id: UUID
    workflow_definition_id: UUID
    workflow_definition_version: int = Field(ge=1)
    conversation_id: UUID
    turn_id: UUID
    source_message_id: UUID
    temporal_workflow_id: str = Field(min_length=1, max_length=255)
    user_request: str = Field(min_length=1)
    max_parallelism: int = Field(ge=1, le=4)
    supervisor: WorkflowAgentSnapshot
    nodes: tuple[WorkflowSnapshotNode, ...] = Field(min_length=1, max_length=20)
    edges: tuple[WorkflowEdgeDraft, ...] = Field(max_length=380)
    loop_bodies: tuple[WorkflowLoopBody, ...] = ()
    status: WorkflowRunStatus = WorkflowRunStatus.PENDING
    created_at: datetime


class WorkflowNodeStatusResponse(WorkflowSchema):
    phase_key: str
    agent_definition_id: UUID
    agent_name: str
    status: str
    attempt_count: int
    error_code: str | None
    started_at: datetime | None
    completed_at: datetime | None


class WorkflowNodeDiagnosticResponse(WorkflowNodeStatusResponse):
    output: str | None
    internal_error: str | None
    diagnostic_expires_at: datetime | None
    output_expired: bool


class WorkflowRunSafeResponse(WorkflowSchema):
    id: UUID
    workflow_definition_id: UUID
    workflow_definition_version: int
    conversation_id: UUID
    turn_id: UUID
    status: WorkflowRunStatus
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    nodes: tuple[WorkflowNodeStatusResponse, ...]


class WorkflowRunDiagnosticResponse(WorkflowRunSafeResponse):
    internal_error: str | None
    nodes: tuple[WorkflowNodeDiagnosticResponse, ...]


class WorkflowCancelResponse(WorkflowSchema):
    run_id: UUID
    accepted: bool = True


class WorkflowDecisionRequest(WorkflowSchema):
    decision: Literal["approve", "reject"]


class WorkflowDecisionResponse(WorkflowSchema):
    run_id: UUID
    node_key: str
    accepted: bool = True


class WorkflowAuthorizeStepRequest(WorkflowSchema):
    phase: Literal["supervisor_prepare", "node", "supervisor_synthesize"]
    node_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")

    @model_validator(mode="after")
    def validate_phase_node_key(self) -> "WorkflowAuthorizeStepRequest":
        if (self.phase == "node") != (self.node_key is not None):
            raise ValueError("nodeKey is required only for node phase")
        return self


class WorkflowStepDecisionResponse(WorkflowSchema):
    run_id: UUID
    phase: Literal["supervisor_prepare", "node", "supervisor_synthesize"]
    node_key: str | None


class WorkflowTransitionRequest(WorkflowSchema):
    status: WorkflowRunStatus
    error_code: str | None = Field(default=None, max_length=64)


class WorkflowNodeResultRequest(WorkflowSchema):
    status: Literal[
        "pending",
        "running",
        "waiting_human",
        "completed",
        "failed",
        "skipped",
        "cancelled",
    ]
    output: str | None = None
    error_code: str | None = Field(default=None, max_length=64)
