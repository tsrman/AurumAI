"""Native-tools chat completion requests from a Hermes Agent sandbox.

The sandbox is less trusted than the runner: messages and tools are bounded,
images may only be inline data URLs (a remote URL would make the provider fetch
it, a channel around the sandbox's closed egress), and workflow correlation is
never taken from the body. Unknown top-level OpenAI parameters are ignored, not
forwarded: only the fields below reach the gateway.
"""

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.config import settings

TOOL_NAME_PATTERN = r"^[A-Za-z0-9_.:-]{1,64}$"
MAX_TOOL_SCHEMA_BYTES = 64 * 1024
MAX_TOOL_ARGUMENTS_CHARS = 1024 * 1024
MAX_TOOL_CALLS_PER_MESSAGE = 128
IMAGE_DATA_URL_PATTERN = r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/]+={0,2}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HermesFunction(_Strict):
    name: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str | None = Field(default=None, max_length=8192)
    parameters: dict[str, Any] | None = None
    strict: bool | None = None

    @field_validator("parameters")
    @classmethod
    def bounded_schema(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and len(json.dumps(value)) > MAX_TOOL_SCHEMA_BYTES:
            raise ValueError("tool parameters schema is too large")
        return value


class HermesTool(_Strict):
    type: Literal["function"]
    function: HermesFunction


class HermesToolCallFunction(_Strict):
    name: str = Field(pattern=TOOL_NAME_PATTERN)
    arguments: str = Field(max_length=MAX_TOOL_ARGUMENTS_CHARS)


class HermesToolCall(_Strict):
    id: str = Field(min_length=1, max_length=128)
    type: Literal["function"]
    function: HermesToolCallFunction


class HermesTextPart(_Strict):
    type: Literal["text"]
    text: str


class HermesImageUrl(_Strict):
    url: str = Field(pattern=IMAGE_DATA_URL_PATTERN)
    detail: Literal["auto", "low", "high"] | None = None

    @field_validator("url")
    @classmethod
    def bounded_size(cls, value: str) -> str:
        # Base64 is 4/3 of the bytes; the decoded image is checked exactly later.
        if len(value) > settings.IMAGES_UPLOAD_MAX_BYTES * 4 // 3 + 64:
            raise ValueError("image is too large")
        return value


class HermesImagePart(_Strict):
    type: Literal["image_url"]
    image_url: HermesImageUrl


HermesContentPart = Annotated[HermesTextPart | HermesImagePart, Field(discriminator="type")]


class HermesMessage(BaseModel):
    # Clients replay assistant messages with extra keys (`refusal`, reasoning
    # content); they are dropped, since gateway_messages rebuilds each message
    # from the fields below only.
    model_config = ConfigDict(extra="ignore")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[HermesContentPart] | None = None
    name: str | None = Field(default=None, max_length=64)
    tool_calls: list[HermesToolCall] | None = Field(
        default=None, max_length=MAX_TOOL_CALLS_PER_MESSAGE
    )
    tool_call_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def role_shape(self) -> "HermesMessage":
        if self.tool_calls and self.role != "assistant":
            raise ValueError("only assistant messages may carry tool_calls")
        if (self.role == "tool") != (self.tool_call_id is not None):
            raise ValueError("tool messages, and only they, require tool_call_id")
        if self.content is None and not (self.role == "assistant" and self.tool_calls):
            raise ValueError("content may be null only on an assistant tool call")
        if isinstance(self.content, list) and self.role != "user":
            if any(isinstance(part, HermesImagePart) for part in self.content):
                raise ValueError("only user messages may carry images")
        return self


class HermesNamedToolChoiceFunction(_Strict):
    name: str = Field(pattern=TOOL_NAME_PATTERN)


class HermesNamedToolChoice(_Strict):
    type: Literal["function"]
    function: HermesNamedToolChoiceFunction


class HermesCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = Field(min_length=1, max_length=255)
    messages: list[HermesMessage] = Field(min_length=1)
    tools: list[HermesTool] | None = None
    tool_choice: Literal["none", "auto", "required"] | HermesNamedToolChoice | None = None
    parallel_tool_calls: bool | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=200_000)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=200_000)
    stop: str | list[str] | None = None
    seed: int | None = None

    @model_validator(mode="after")
    def bounded(self) -> "HermesCompletionRequest":
        if len(self.messages) > settings.HERMES_MAX_MESSAGES:
            raise ValueError("too many messages")
        if self.tools is not None and len(self.tools) > settings.HERMES_MAX_TOOLS:
            raise ValueError("too many tools")
        if self.tools is not None:
            names = [tool.function.name for tool in self.tools]
            if len(set(names)) != len(names):
                raise ValueError("tool names must be unique")
        if isinstance(self.stop, list) and len(self.stop) > 4:
            raise ValueError("at most four stop sequences")
        return self

    def gateway_fields(self) -> dict[str, Any]:
        """Optional generation parameters forwarded as sent."""
        fields = self.model_dump(
            include={
                "tool_choice",
                "parallel_tool_calls",
                "temperature",
                "top_p",
                "max_tokens",
                "max_completion_tokens",
                "stop",
                "seed",
            },
            exclude_none=True,
        )
        if self.tools is not None:
            fields["tools"] = [tool.model_dump(exclude_none=True) for tool in self.tools]
        return fields


class HermesSessionTokenResponse(BaseModel):
    token: str
    sessionId: str
    expiresAt: str
