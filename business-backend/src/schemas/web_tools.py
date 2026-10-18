"""Strict model-facing contracts for the bounded built-in web tools."""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel


WEB_TOOLS_CONTRACT_VERSION = 1


WebToolName = Literal["web_search", "web_read"]
WebTimeRange = Literal["day", "week", "month", "year"]
WebErrorCode = Literal[
    "web_disabled",
    "web_access_denied",
    "web_limit",
    "web_timeout",
    "web_unavailable",
    "web_url_blocked",
    "web_too_large",
    "unsupported_document",
    # Platform-side only: the isolated web service can never observe a turn
    # cancellation, so this code is never accepted from it over the wire.
    "web_cancelled",
]


class WebSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WebSearchRequest(WebSchema):
    query: str = Field(min_length=1, max_length=2000, strict=True)
    language: str | None = Field(
        default=None,
        min_length=2,
        max_length=35,
        pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$",
        strict=True,
    )
    time_range: WebTimeRange | None = None

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class WebReadRequest(WebSchema):
    # Scheme, port, credentials and resolved addresses are security policy,
    # not mere syntax, and are validated by the isolated service before I/O.
    url: str = Field(min_length=1, max_length=8192, strict=True)

    @field_validator("url")
    @classmethod
    def nonblank_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("url must not be blank")
        return value


class WebOperationRequest(WebSchema):
    tool: WebToolName
    args: WebSearchRequest | WebReadRequest

    @model_validator(mode="after")
    def arguments_match_tool(self) -> Self:
        expected = WebSearchRequest if self.tool == "web_search" else WebReadRequest
        if not isinstance(self.args, expected):
            raise ValueError("arguments do not match web tool")
        return self


class WebToolError(WebSchema):
    code: WebErrorCode


class WebSearchServiceHit(WebSchema):
    title: str = Field(min_length=1, max_length=2000, strict=True)
    url: str = Field(min_length=1, max_length=8192, strict=True)
    snippet: str = Field(max_length=4000, strict=True)
    published_at: datetime | None


class WebSearchServiceResponse(WebSchema):
    results: list[WebSearchServiceHit] = Field(max_length=5)


class WebReadServiceResponse(WebSchema):
    requested_url: str = Field(min_length=1, max_length=8192, strict=True)
    final_url: str = Field(min_length=1, max_length=8192, strict=True)
    content_type: str = Field(min_length=1, max_length=255, strict=True)
    text: str = Field(min_length=1, max_length=50000, strict=True)
    title: str | None = Field(default=None, max_length=2000, strict=True)
    truncated: bool
    page_count: int | None = Field(default=None, ge=0, strict=True)


class WebSourceResponse(WebSchema):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        from_attributes=True,
    )

    source_id: UUID
    kind: Literal["search", "read"]
    requested_url: str | None
    final_url: str
    title: str | None
    content_type: str | None
    snippet: str | None = Field(default=None, max_length=4000)
    published_at: datetime | None
    retrieved_at: datetime
    truncated: bool
    page_count: int | None
