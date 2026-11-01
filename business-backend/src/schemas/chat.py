from typing import Any, List, Literal, Optional
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.services.file_uploads import MAX_FILES_PER_MESSAGE


MAX_MESSAGE_IMAGES = 4


class ChatMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    role: str
    content: str
    # Uploaded images (POST /conversations/{id}/images) this message carries.
    image_ids: list[UUID] = Field(
        default_factory=list, alias="imageIds", max_length=MAX_MESSAGE_IMAGES
    )
    # Uploaded files (POST /conversations/{id}/files) for Hermes's workspace.
    file_ids: list[UUID] = Field(default_factory=list, alias="fileIds", max_length=MAX_FILES_PER_MESSAGE)


class ChatOptions(BaseModel):
    temperature: float = 0.7
    max_tokens: int = 4096
    tools: Optional[List[str]] = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    conversation_id: Optional[str] = None
    # chat_stream does request.messages[-1] unconditionally — an empty list
    # raised an unhandled IndexError (500) instead of a clean validation
    # error.
    messages: List[ChatMessage] = Field(min_length=1)
    model: str
    options: ChatOptions = Field(default_factory=ChatOptions)
    workflow_id: UUID | None = Field(default=None, alias="workflowId")
    # A system agent instead of the plain model: "hermes" runs Hermes Agent
    # in its sandbox on the organization's Hermes model (`model` is ignored).
    agent: Literal["hermes"] | None = None
    # An agent of the administrator's (GET /chat/agent-definitions): its model,
    # instructions, skills and knowledge bases scope the turn (`model` is ignored).
    agent_definition_id: UUID | None = Field(default=None, alias="agentDefinitionId")

    @model_validator(mode="after")
    def one_agent_or_workflow(self) -> "ChatRequest":
        chosen = [self.agent, self.agent_definition_id, self.workflow_id]
        if sum(item is not None for item in chosen) > 1:
            raise ValueError("choose one of an agent, an agent definition or a workflow")
        return self


class ChatCompletionMessage(BaseModel):
    role: str
    content: str
    # Conversation images the proxy inlines for vision models; the runner
    # forwards them from user messages only.
    image_ids: list[UUID] = Field(default_factory=list, max_length=MAX_MESSAGE_IMAGES)

    @model_validator(mode="after")
    def images_only_on_user_messages(self) -> "ChatCompletionMessage":
        if self.image_ids and self.role != "user":
            raise ValueError("only user messages may carry images")
        return self


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[ChatCompletionMessage]
    stream: bool = False
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 4096
    workflow_run_id: UUID | None = None
    workflow_phase: Literal[
        "supervisor_prepare", "node", "supervisor_synthesize"
    ] | None = None
    workflow_node_key: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]{0,63}$",
    )

    @model_validator(mode="after")
    def validate_workflow_correlation(self) -> "ChatCompletionRequest":
        if self.workflow_run_id is None:
            if self.workflow_phase is not None or self.workflow_node_key is not None:
                raise ValueError("workflow correlation requires workflow_run_id")
            return self
        if self.workflow_phase == "node" and self.workflow_node_key is not None:
            return self
        if (
            self.workflow_phase in {"supervisor_prepare", "supervisor_synthesize"}
            and self.workflow_node_key is None
        ):
            return self
        raise ValueError("invalid workflow phase/node correlation")


class PersistConversationMessageRequest(BaseModel):
    role: Literal["assistant", "tool"]
    content: str
    model: Optional[str] = None
    turn_id: UUID
    tool_calls: Optional[list[Any] | dict[str, Any]] = None
    tool_results: Optional[dict[str, Any]] = None


class UploadedImageResponse(BaseModel):
    imageId: str
    contentType: str
    width: int | None
    height: int | None


class PersistedImageClaims(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    image_ids: list[UUID] = Field(default_factory=list, alias="imageIds", max_length=20)


class PersistedWebSourceClaims(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    source_ids: list[UUID] = Field(default_factory=list, alias="sourceIds", max_length=100)
    unsupported_source_ids: list[str] = Field(
        default_factory=list,
        alias="unsupportedSourceIds",
        max_length=100,
    )

    @field_validator("unsupported_source_ids")
    @classmethod
    def validate_unsupported_ids(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 128 for value in values):
            raise ValueError("invalid unsupported source id")
        return values


class MessageScoreRequest(BaseModel):
    score: Optional[float] = None
    reason: Optional[str] = None
