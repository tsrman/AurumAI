from datetime import datetime
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from src.models.workflow_template import WorkflowTemplateStatus
from src.schemas.agent_definition import AgentDefinitionCreate
from src.schemas.workflow import WorkflowDefinitionResponse, WorkflowEdgeDraft, WorkflowNodeDraft
from src.services.workflow_graph import validate_workflow_graph


class TemplateSchema(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class TemplateAgent(AgentDefinitionCreate):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class TemplateNode(TemplateSchema):
    node_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    display_name: str = Field(min_length=1, max_length=255)
    agent_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
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


class TemplateSnapshot(TemplateSchema):
    schema_version: Literal[1]
    supervisor_agent_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    max_parallelism: int = Field(ge=1, le=4)
    agents: list[TemplateAgent] = Field(min_length=1, max_length=21)
    nodes: list[TemplateNode] = Field(min_length=1, max_length=20)
    edges: list[WorkflowEdgeDraft] = Field(max_length=380)

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_known_integer_schema_version(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schemaVersion must be the integer 1")
        return value

    @model_validator(mode="after")
    def validate_configuration(self) -> "TemplateSnapshot":
        agent_keys = [agent.key for agent in self.agents]
        if len(agent_keys) != len(set(agent_keys)):
            raise ValueError("agent keys must be unique")

        referenced_agent_keys = {
            self.supervisor_agent_key,
            *(node.agent_key for node in self.nodes),
        }
        if set(agent_keys) != referenced_agent_keys:
            raise ValueError("agent keys must exactly match graph references")

        temporary_agent_ids = {
            key: uuid5(NAMESPACE_URL, key) for key in referenced_agent_keys
        }
        workflow_nodes = [
            WorkflowNodeDraft(
                node_key=node.node_key,
                display_name=node.display_name,
                agent_definition_id=temporary_agent_ids[node.agent_key],
                instruction=node.instruction,
                position_x=node.position_x,
                position_y=node.position_y,
                requires_confirmation=node.requires_confirmation,
            )
            for node in self.nodes
        ]
        validate_workflow_graph(
            workflow_nodes,
            self.edges,
            max_nodes=20,
        )
        return self


class TemplateCaptureRequest(TemplateSchema):
    source_workflow_id: UUID
    source_workflow_version: int = Field(strict=True, ge=1)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("name must be a string")
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


class TemplateReplaceRequest(TemplateCaptureRequest):
    version: int = Field(strict=True, ge=1)


class TemplateVersionRequest(TemplateSchema):
    version: int = Field(strict=True, ge=1)


class TemplateInstantiateRequest(TemplateVersionRequest):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("name must be a string")
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized


class TemplateListItem(TemplateSchema):
    id: UUID
    name: str
    description: str | None
    status: WorkflowTemplateStatus
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class TemplateResponse(TemplateListItem):
    org_id: UUID
    created_by: UUID | None
    updated_by: UUID | None
    snapshot: TemplateSnapshot


class TemplateInstantiationResponse(TemplateSchema):
    workflow: WorkflowDefinitionResponse
    agent_ids: list[UUID]
