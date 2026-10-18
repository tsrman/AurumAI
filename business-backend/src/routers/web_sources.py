"""Owner-only web source history for a conversation turn."""

import uuid
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.authorization_dependencies import authorization_subject
from src.core.dependencies import get_org_db_session
from src.schemas.common import ApiResponse, Meta
from src.schemas.web_tools import WebSourceResponse
from src.services.web_tools import WebToolExecutionError, web_tools_service


router = APIRouter()


def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(UTC).isoformat(),
    )


@router.get("/conversations/{conversation_id}/turns/{turn_id}/sources")
async def get_sources(
    conversation_id: UUID,
    turn_id: UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[list[WebSourceResponse]]:
    try:
        sources = await web_tools_service.list_sources(
            session,
            authorization_subject(user),
            conversation_id,
            turn_id,
        )
    except WebToolExecutionError as exc:
        if exc.code == "interaction_not_found":
            raise HTTPException(status_code=404, detail="Sources not found") from exc
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
    return ApiResponse(data=sources, meta=_meta())
