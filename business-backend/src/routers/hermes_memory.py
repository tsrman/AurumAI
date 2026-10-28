"""A user's own Hermes Agent memory, in their settings (docs/HERMES_AGENT.md,
phase 5a): read it, or clear it. Nobody else's is reachable."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.authorization_dependencies import authorization_subject
from src.core.dependencies import get_org_db_session
from src.schemas.common import ApiResponse, Meta
from src.services import hermes_memory

router = APIRouter()


class HermesMemoryResponse(BaseModel):
    # Hermes's notes (MEMORY.md) and its profile of the user (USER.md).
    memory: str
    user: str


def _meta() -> Meta:
    return Meta(requestId=f"req_{uuid.uuid4().hex}", timestamp=datetime.now(timezone.utc).isoformat())


@router.get("/hermes/memory", response_model=ApiResponse[HermesMemoryResponse])
async def my_hermes_memory(
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[HermesMemoryResponse]:
    subject = authorization_subject(user)
    memory = await hermes_memory.read(session, subject.org_id, subject.user_id)
    return ApiResponse(
        data=HermesMemoryResponse(memory=memory["memory"].content, user=memory["user"].content), meta=_meta()
    )


@router.delete("/hermes/memory", status_code=status.HTTP_204_NO_CONTENT)
async def clear_my_hermes_memory(
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> Response:
    """Hermes starts the next session without it; a turn running now cannot
    write back what it read before."""
    subject = authorization_subject(user)
    await hermes_memory.clear(session, subject.org_id, subject.user_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
