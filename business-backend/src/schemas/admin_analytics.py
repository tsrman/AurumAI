from datetime import date
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class AnalyticsSchema(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


NonNegativeInt64 = Annotated[int, Field(ge=0, le=2**63 - 1)]


class UsagePeriodResponse(AnalyticsSchema):
    date_from: date = Field(serialization_alias="from")
    date_to: date = Field(serialization_alias="to")
    timezone: str = "UTC"


class UsageLimitsResponse(AnalyticsSchema):
    request_limit: NonNegativeInt64 | None
    token_limit: NonNegativeInt64 | None


class UsageQuotaResponse(AnalyticsSchema):
    """Monthly quota consumption — never derived from the report's period."""

    period_start: date
    requests_used: NonNegativeInt64
    tokens_used: NonNegativeInt64
    requests_remaining: NonNegativeInt64 | None
    tokens_remaining: NonNegativeInt64 | None


class UsageTotalsResponse(AnalyticsSchema):
    requests: NonNegativeInt64
    prompt_tokens: NonNegativeInt64
    completion_tokens: NonNegativeInt64
    total_tokens: NonNegativeInt64
    unaccounted_requests: NonNegativeInt64
    images: NonNegativeInt64


class DailyUsageResponse(UsageTotalsResponse):
    date: date


class UserUsageResponse(UsageTotalsResponse):
    user_id: UUID
    email: str
    full_name: str | None


class ModelUsageResponse(UsageTotalsResponse):
    model: str


class AdminUsageResponse(AnalyticsSchema):
    period: UsagePeriodResponse
    limits: UsageLimitsResponse
    quota: UsageQuotaResponse
    totals: UsageTotalsResponse
    daily: list[DailyUsageResponse]
    by_user: list[UserUsageResponse]
    # How many users had usage at all, so a capped `by_user` can say so.
    by_user_total: NonNegativeInt64
    by_model: list[ModelUsageResponse]
