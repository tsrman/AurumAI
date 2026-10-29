from datetime import datetime
from uuid import UUID

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel


class _AgentDefinitionFields(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    @field_validator("name", "model_id", "system_prompt", check_fields=False)
    @classmethod
    def normalize_required_text(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("value must not be null")
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("description", check_fields=False)
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("skill_ids", "knowledge_base_ids", check_fields=False)
    @classmethod
    def reject_duplicate_ids(cls, value: list[UUID] | None) -> list[UUID]:
        if value is None:
            raise ValueError("dependency IDs must not be null")
        if len(value) != len(set(value)):
            raise ValueError("dependency IDs must be unique")
        return value


class AgentDefinitionCreate(_AgentDefinitionFields):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)
    model_id: str = Field(min_length=1, max_length=255)
    system_prompt: str = Field(min_length=1, max_length=50_000)
    # None means "inherit the organization default"; defaults to 0.3 (a
    # sensible starting point) if omitted, but a caller may pass null
    # explicitly to start the agent inheriting from creation.
    temperature: float | None = Field(default=0.3, ge=0, le=2)
    skill_ids: list[UUID] = Field(default_factory=list, max_length=100)
    knowledge_base_ids: list[UUID] = Field(default_factory=list, max_length=100)
    # Fixed at creation. "hermes": a workflow node run by Hermes Agent, which
    # brings the platform's tools and its own skills instead of these.
    kind: Literal["agent", "hermes"] = "agent"

    @model_validator(mode="after")
    def hermes_has_no_tools(self) -> "AgentDefinitionCreate":
        if self.kind == "hermes" and (self.skill_ids or self.knowledge_base_ids):
            raise ValueError("a Hermes Agent definition takes no skills or knowledge bases")
        return self


class AgentDefinitionUpdate(_AgentDefinitionFields):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)
    model_id: str | None = Field(default=None, min_length=1, max_length=255)
    system_prompt: str | None = Field(default=None, min_length=1, max_length=50_000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    skill_ids: list[UUID] | None = Field(default=None, max_length=100)
    knowledge_base_ids: list[UUID] | None = Field(default=None, max_length=100)


class AgentDefinitionResponse(BaseModel):
    id: UUID
    name: str
    description: str | None = None
    modelId: str
    systemPrompt: str
    # None means "inherit the organization default" — the raw stored value,
    # not resolved, so the constructor form can show whether this agent
    # currently overrides it.
    temperature: float | None
    skillIds: list[UUID]
    knowledgeBaseIds: list[UUID]
    kind: Literal["agent", "hermes"] = "agent"
    canWrite: bool
    createdAt: datetime
    updatedAt: datetime
