"""Public question payloads contain data only, never runtime or authorization fields."""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel


ExecutionPhase = Literal["ordinary", "node", "supervisor_prepare", "supervisor_synthesize"]


class InteractionSchema(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        from_attributes=True,
    )


class TurnLimits(InteractionSchema):
    question_timeout_seconds: int = Field(default=3600, ge=1, strict=True)
    question_total: int = Field(default=10, ge=1, strict=True)
    question_open: int = Field(default=4, ge=1, strict=True)
    web_search_total: int = Field(default=10, ge=0, strict=True)
    web_read_total: int = Field(default=20, ge=0, strict=True)
    image_total: int = Field(default=4, ge=0, strict=True)


class QuestionOption(InteractionSchema):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    label: str = Field(min_length=1, max_length=200)

    @field_validator("label")
    @classmethod
    def nonblank_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("option label must not be blank")
        return value


class QuestionRequest(InteractionSchema):
    text: str = Field(min_length=1, max_length=4000)
    options: list[QuestionOption] = Field(default_factory=list, max_length=8)

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question text must not be blank")
        return value

    @model_validator(mode="after")
    def unique_option_ids(self) -> Self:
        if len({option.id for option in self.options}) != len(self.options):
            raise ValueError("option IDs must be unique")
        return self


class AnswerRequest(InteractionSchema):
    text: str | None = Field(default=None, min_length=1, max_length=16000)
    option_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")

    @model_validator(mode="after")
    def exactly_one_answer(self) -> Self:
        if (self.text is None) == (self.option_id is None):
            raise ValueError("supply exactly one of text or optionId")
        if self.text is not None and not self.text.strip():
            raise ValueError("answer text must not be blank")
        return self


class SubmitAnswerRequest(InteractionSchema):
    idempotency_key: UUID
    answer: AnswerRequest


class QuestionCreateRequest(InteractionSchema):
    invocation_id: UUID
    question: QuestionRequest


class ExecutionRegistrationRequest(InteractionSchema):
    workflow_id: str = Field(min_length=1, max_length=255)
    run_id: UUID
    task_queue: str = Field(min_length=1, max_length=255)
    phase: ExecutionPhase
    node_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    agent_definition_id: UUID | None = None
    iteration: int = Field(default=1, ge=1, strict=True)

    @field_validator("workflow_id", "task_queue")
    @classmethod
    def nonblank_runtime_address(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("runtime address must not be blank")
        return value

    @model_validator(mode="after")
    def phase_shape(self) -> Self:
        if self.phase == "ordinary":
            if self.node_key is not None or self.agent_definition_id is not None:
                raise ValueError("ordinary execution has no agent target")
        elif self.phase == "node":
            if self.node_key is None or self.agent_definition_id is None:
                raise ValueError("node execution requires node and agent")
        elif self.node_key is not None or self.agent_definition_id is None:
            raise ValueError("supervisor execution requires only an agent")
        return self


class ExecutionResponse(InteractionSchema):
    execution_id: UUID
    turn_id: UUID
    workflow_id: str
    run_id: UUID
    task_queue: str
    phase: ExecutionPhase
    node_key: str | None
    agent_definition_id: UUID | None
    iteration: int


class QuestionResponse(InteractionSchema):
    question_id: UUID
    turn_id: UUID
    text: str
    options: list[QuestionOption]
    status: Literal["pending", "answered", "expired", "cancelled"]
    version: int = Field(ge=1)
    expires_at: datetime
    created_at: datetime
    phase: ExecutionPhase
    node_key: str | None
    agent_name: str | None
    answer: AnswerRequest | None
    answered_at: datetime | None


class TurnQuestionState(InteractionSchema):
    question_id: UUID
    status: Literal["pending", "answered", "expired", "cancelled"]
    version: int = Field(ge=1)


class TurnSnapshot(InteractionSchema):
    turn_id: UUID
    status: Literal["active", "completed", "failed", "cancelled"]
    version: int = Field(ge=1)
    questions: list[TurnQuestionState]
