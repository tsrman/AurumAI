"""Organization settings for Hermes Agent (docs/HERMES_AGENT.md).

The internet egress mode and the model Hermes runs on. The operator's maximum
is shown read-only; a mode above the maximum is refused rather than silently
lowered. The model must be one the organization grants; every Hermes call
checks the user's own grant again.
"""

import uuid
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo
from src.core.authorization_dependencies import require_org_admin
from src.services.model_access import ensure_model_allowed
from src.core.config import settings
from src.core.dependencies import get_current_org_id, get_org_db_session
from src.schemas.common import ApiResponse, Meta
from src.services.hermes_egress import (
    EgressModeAboveMaximum,
    organization_egress_mode,
    organization_hermes_model,
    set_organization_egress_mode,
    set_organization_hermes_model,
)

router = APIRouter()
Mode = Literal["none", "packages", "public"]


class HermesSettingsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool
    egress_mode: Mode = Field(serialization_alias="egressMode")
    egress_max_mode: Mode = Field(serialization_alias="egressMaxMode")
    model: str | None


class HermesSettingsUpdate(BaseModel):
    """Fields left out are kept; `model: null` turns Hermes off for the organization."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    egress_mode: Mode | None = Field(default=None, alias="egressMode")
    model: str | None = Field(default=None, min_length=1, max_length=255)


def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


async def _response(session: AsyncSession, org_id: UUID) -> HermesSettingsResponse:
    return HermesSettingsResponse(
        enabled=settings.HERMES_ENABLED,
        egress_mode=await organization_egress_mode(session, org_id),
        egress_max_mode=settings.HERMES_EGRESS_MAX_MODE,
        model=await organization_hermes_model(session, org_id),
    )


@router.get("/settings/hermes", response_model=ApiResponse[HermesSettingsResponse])
async def hermes_settings(
    user: UserInfo = Depends(require_org_admin),
    org_id: UUID = Depends(get_current_org_id),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[HermesSettingsResponse]:
    return ApiResponse(data=await _response(session, org_id), meta=_meta())


@router.put("/settings/hermes", response_model=ApiResponse[HermesSettingsResponse])
async def update_hermes_settings(
    data: HermesSettingsUpdate,
    user: UserInfo = Depends(require_org_admin),
    org_id: UUID = Depends(get_current_org_id),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[HermesSettingsResponse]:
    if data.egress_mode is not None:
        try:
            await set_organization_egress_mode(session, org_id, data.egress_mode)
        except EgressModeAboveMaximum:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Egress mode is above the operator's maximum",
            ) from None
    if "model" in data.model_fields_set:
        if data.model is not None:
            await ensure_model_allowed(
                session, str(org_id), data.model, user_id=user.user_id, role=user.role
            )
        await set_organization_hermes_model(session, org_id, data.model)
    await session.commit()
    return ApiResponse(data=await _response(session, org_id), meta=_meta())
