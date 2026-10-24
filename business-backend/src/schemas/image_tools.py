"""Strict model-facing contracts for the built-in image tools."""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel


ImageToolName = Literal["generate_image", "edit_image"]
ImageSize = Literal["auto", "1024x1024", "1024x1536", "1536x1024"]
ImageErrorCode = Literal[
    "image_disabled",
    "image_access_denied",
    "image_limit",
    "image_timeout",
    "image_unavailable",
    "image_rejected",
    "image_not_found",
    "image_cancelled",
]

IMAGE_TOOL_KINDS: dict[str, str] = {
    "generate_image": "image_generate",
    "edit_image": "image_edit",
}


class ImageSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _PromptArgs(ImageSchema):
    prompt: str = Field(min_length=1, max_length=4000, strict=True)
    size: ImageSize = "auto"

    @field_validator("prompt")
    @classmethod
    def nonblank_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be blank")
        return value


class GenerateImageRequest(_PromptArgs):
    pass


class EditImageRequest(_PromptArgs):
    # Only an image of the same conversation, never a URL or path.
    image_id: UUID


class ImageOperationRequest(ImageSchema):
    tool: ImageToolName
    args: EditImageRequest | GenerateImageRequest

    @model_validator(mode="after")
    def arguments_match_tool(self) -> Self:
        expected = GenerateImageRequest if self.tool == "generate_image" else EditImageRequest
        if type(self.args) is not expected:
            raise ValueError("arguments do not match image tool")
        return self


class ImageToolError(ImageSchema):
    code: ImageErrorCode


class ConversationImageResponse(ImageSchema):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    image_id: UUID
    origin: Literal["generated", "uploaded"]
    content_type: str
    width: int | None
    height: int | None
    source_image_id: UUID | None
