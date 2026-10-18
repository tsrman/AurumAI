from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import get_current_user, UserInfo
from src.core.config import settings
from src.core.dependencies import get_org_db_session
from src.schemas.common import ApiResponse, Meta
from src.services.model_access import allowed_model_ids
# Re-exported: the marker list and this predicate moved to the shared catalog
# so services on the authorization path can filter to chat models without
# importing a router.
from src.services.model_catalog import is_chat_model as is_chat_model

router = APIRouter()


def _meta() -> Meta:
    return Meta(requestId="req_", timestamp=datetime.now(timezone.utc).isoformat())


def _mock_models() -> list[dict[str, Any]]:
    return [
        {"id": "gpt-4o-mini", "object": "model"},
        {"id": "gpt-4o", "object": "model"},
        {"id": "claude-3-5-sonnet", "object": "model"},
    ]


async def list_gateway_models() -> list[dict[str, Any]]:
    """Return chat-capable models from the configured LLM gateway."""
    if settings.DEBUG and not settings.GATEWAY_API_KEY:
        return _mock_models()

    try:
        async with httpx.AsyncClient(
            timeout=30.0, verify=settings.gateway_verify
        ) as client:
            response = await client.get(
                f"{settings.LLM_GATEWAY_URL}/v1/models",
                headers={
                    "Authorization": f"Bearer {settings.GATEWAY_API_KEY}",
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return _mock_models()

    models = payload.get("data", []) if isinstance(payload, dict) else payload
    if not isinstance(models, list):
        models = []
    return [
        m
        for m in models
        if not isinstance(m, dict) or is_chat_model(str(m.get("id") or ""))
    ]


@router.get("/models")
async def list_models(
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[list[dict[str, Any]]]:
    """Return gateway models readable through Authorization v2 grants."""
    models = await list_gateway_models()
    model_ids = {m.get("id") for m in models if isinstance(m, dict)}
    visible_ids = set(
        await allowed_model_ids(
            session,
            user.org_id,
            [i for i in model_ids if i],
            user_id=user.user_id,
            role=user.role,
        )
    )
    models = [
        m
        for m in models
        if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"] in visible_ids
    ]
    return ApiResponse(data=models, meta=_meta())
