import time
import uuid
from datetime import date, datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo
from src.core.authorization_dependencies import require_org_admin
from src.core.authorization_types import ResourceRef, ResourceType
from src.core.dependencies import get_current_org_id, get_org_db_session
from src.core.logging import logger
from src.models.user import User
from src.schemas.admin_analytics import (
    AdminUsageResponse,
    DailyUsageResponse,
    ModelUsageResponse,
    UsageLimitsResponse,
    UsagePeriodResponse,
    UsageQuotaResponse,
    UsageTotalsResponse,
    UserUsageResponse,
)
from src.schemas.common import ApiResponse, Meta
from src.services.usage_analytics import (
    UsageAnalyticsReport,
    UsageAnalyticsService,
    UsageTotals,
    usage_analytics_service,
)


router = APIRouter()


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def get_usage_analytics_service() -> UsageAnalyticsService:
    return usage_analytics_service


def _remaining(limit: int | None, used: int) -> int | None:
    if limit is None:
        return None
    return max(0, limit - used)


def _totals(value: UsageTotals) -> UsageTotalsResponse:
    return UsageTotalsResponse(
        requests=value.requests,
        prompt_tokens=value.prompt_tokens,
        completion_tokens=value.completion_tokens,
        total_tokens=value.total_tokens,
        unaccounted_requests=value.unaccounted_requests,
        images=value.images,
    )


def _response(
    report: UsageAnalyticsReport,
    date_from: date,
    date_to: date,
) -> AdminUsageResponse:
    return AdminUsageResponse(
        period=UsagePeriodResponse(date_from=date_from, date_to=date_to),
        limits=UsageLimitsResponse(
            request_limit=report.request_limit,
            token_limit=report.token_limit,
        ),
        quota=UsageQuotaResponse(
            period_start=report.quota.period_start,
            requests_used=report.quota.requests_used,
            tokens_used=report.quota.tokens_used,
            requests_remaining=_remaining(
                report.request_limit, report.quota.requests_used
            ),
            tokens_remaining=_remaining(report.token_limit, report.quota.tokens_used),
        ),
        totals=_totals(report.totals),
        daily=[
            DailyUsageResponse(date=row.date, **_totals(row.totals).model_dump())
            for row in report.daily
        ],
        by_user=[
            UserUsageResponse(
                user_id=row.user_id,
                email=row.email,
                full_name=row.full_name,
                **_totals(row.totals).model_dump(),
            )
            for row in report.by_user
        ],
        by_user_total=report.by_user_total,
        by_model=[
            ModelUsageResponse(
                model=row.model,
                **_totals(row.totals).model_dump(),
            )
            for row in report.by_model
        ],
    )


@router.get("/analytics/usage", response_model=ApiResponse[AdminUsageResponse])
async def admin_usage(
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    user_id: UUID | None = Query(default=None, alias="userId"),
    model: str | None = Query(default=None, max_length=255),
    user: UserInfo = Depends(require_org_admin),
    org_id: UUID = Depends(get_current_org_id),
    session: AsyncSession = Depends(get_org_db_session),
    service: UsageAnalyticsService = Depends(get_usage_analytics_service),
) -> ApiResponse[AdminUsageResponse]:
    today = _utc_today()
    resolved_to = date_to or today
    resolved_from = date_from or today.replace(day=1)
    days = (resolved_to - resolved_from).days + 1
    if days < 1 or days > 366 or resolved_to == date.max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Analytics period must contain between 1 and 366 days",
        )
    if model is not None:
        try:
            ResourceRef(ResourceType.MODEL, model)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Invalid model identifier",
            ) from exc

    if user_id is not None and not await session.scalar(
        select(User.id).where(User.id == user_id, User.org_id == org_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    request_id = f"req_{uuid.uuid4().hex}"
    started = time.perf_counter()
    report = await service.report(
        session,
        org_id,
        resolved_from,
        resolved_to,
        user_id=user_id,
        model=model,
    )
    logger.info(
        "admin_usage_report_generated",
        request_id=request_id,
        org_id=str(org_id),
        date_from=resolved_from.isoformat(),
        date_to=resolved_to.isoformat(),
        has_user_filter=user_id is not None,
        has_model_filter=model is not None,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
    )
    return ApiResponse(
        data=_response(report, resolved_from, resolved_to),
        meta=Meta(
            requestId=request_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
        ),
    )
