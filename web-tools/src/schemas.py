"""Version-one bounded request DTOs; no caller-controlled transport fields."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


WEB_TOOLS_CONTRACT_VERSION = 1


class WebSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchRequest(WebSchema):
    query: str = Field(min_length=1, max_length=2000, strict=True)
    language: str | None = Field(
        default=None,
        min_length=2,
        max_length=35,
        pattern=r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$",
        strict=True,
    )
    time_range: Literal["day", "week", "month", "year"] | None = None

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class ReadRequest(WebSchema):
    url: str = Field(min_length=1, max_length=8192, strict=True)

    @field_validator("url")
    @classmethod
    def nonblank_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("url must not be blank")
        return value
