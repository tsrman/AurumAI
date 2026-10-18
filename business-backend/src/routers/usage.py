import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from src.core.auth import UserInfo, get_current_user
from src.core.dependencies import get_org_db_session
from src.schemas.common import ApiResponse, Meta
from src.schemas.usage import (
    UsageOrganizationResponse,
    UsageResponse,
    UsageUserResponse,
)
from src.services.quota import QuotaService, get_quota_service

router = APIRouter()


def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _required_uuid(value: str | None, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value or "")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid user {field}",
        ) from exc


def _remaining(limit: int | None, used: int) -> int | None:
    if limit is None:
        return None
    return max(0, limit - used)


@router.get(
    "/usage/me",
    response_model=ApiResponse[UsageResponse],
    # Declared as a route dependency purely for its side effect: it runs
    # sync_keycloak_user and commits, provisioning the org/user rows the way
    # every other business-plane route does. QuotaService opens its own
    # sessions, so the yielded session itself is never needed here — but
    # without this, a dashboard calling /v1/usage/me as the very first request
    # after login hit a missing organization and got a 500 from
    # QuotaInvariantError instead of a zeroed summary.
    dependencies=[Depends(get_org_db_session)],
)
async def get_my_usage(
    user: UserInfo = Depends(get_current_user),
    quota: QuotaService = Depends(get_quota_service),
) -> ApiResponse[UsageResponse]:
    org_id = _required_uuid(user.org_id, "organization id")
    user_id = _required_uuid(user.user_id, "id")
    snapshot = await quota.snapshot(org_id, user_id)

    return ApiResponse(
        data=UsageResponse(
            periodStart=snapshot.period_start.isoformat(),
            periodEnd=snapshot.period_end.isoformat(),
            user=UsageUserResponse(
                requestsUsed=snapshot.user_requests_used,
                tokensUsed=snapshot.user_tokens_used,
            ),
            organization=UsageOrganizationResponse(
                requestsUsed=snapshot.org_requests_used,
                tokensUsed=snapshot.org_tokens_used,
                requestLimit=snapshot.request_limit,
                tokenLimit=snapshot.token_limit,
                requestsRemaining=_remaining(
                    snapshot.request_limit, snapshot.org_requests_used
                ),
                tokensRemaining=_remaining(
                    snapshot.token_limit, snapshot.org_tokens_used
                ),
            ),
        ),
        meta=_meta(),
    )
