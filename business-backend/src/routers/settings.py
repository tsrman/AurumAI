from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone
from uuid import UUID

from src.core.dependencies import get_org_db_session
from src.core.auth import get_current_user, UserInfo
from src.schemas.settings import UserSettingsResponse, UserSettingsUpdate
from src.schemas.common import ApiResponse, Meta
from src.models.user_settings import UserSettings
from src.models.organization_settings import OrganizationSettings

router = APIRouter()


def _meta() -> Meta:
    return Meta(requestId="req_", timestamp=datetime.now(timezone.utc).isoformat())


def _settings_response(
    settings: UserSettings,
    organization_default_model: str | None,
) -> UserSettingsResponse:
    # Columns are nullable (no server_default), but mapped_column(default=...)
    # always populates them on insert — these `or`/`is not None` fallbacks
    # just mirror those same defaults for mypy's benefit, not new behavior.
    return UserSettingsResponse(
        user_id=str(settings.user_id),
        theme=settings.theme or "system",
        language=settings.language or "ru",
        default_model=settings.default_model,
        organization_default_model=organization_default_model,
        code_theme=settings.code_theme or "github-dark",
        enter_to_send=settings.enter_to_send if settings.enter_to_send is not None else True,
        show_timestamps=settings.show_timestamps if settings.show_timestamps is not None else True,
        sidebar_collapsed=settings.sidebar_collapsed if settings.sidebar_collapsed is not None else False,
        notifications=settings.notifications or {},
        shortcuts=settings.shortcuts or {},
    )


async def _organization_default_model(
    session: AsyncSession,
    user: UserInfo,
) -> str | None:
    return await session.scalar(
        select(OrganizationSettings.default_model).where(
            OrganizationSettings.org_id == UUID(user.org_id or "")
        )
    )


@router.get("/me")
async def get_settings(
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[UserSettingsResponse]:
    result = await session.execute(select(UserSettings).where(UserSettings.user_id == user.user_id))
    settings = result.scalar_one_or_none()
    if settings is None:
        settings = UserSettings(user_id=user.user_id)
        session.add(settings)
        await session.commit()
        await session.refresh(settings)
    organization_default = await _organization_default_model(session, user)
    return ApiResponse(
        data=_settings_response(settings, organization_default),
        meta=_meta(),
    )


@router.patch("/me")
async def update_settings(
    data: UserSettingsUpdate,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[UserSettingsResponse]:
    result = await session.execute(select(UserSettings).where(UserSettings.user_id == user.user_id))
    settings = result.scalar_one_or_none()
    if settings is None:
        settings = UserSettings(user_id=user.user_id)
        session.add(settings)

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(settings, key, value)

    await session.commit()
    await session.refresh(settings)
    organization_default = await _organization_default_model(session, user)
    return ApiResponse(
        data=_settings_response(settings, organization_default),
        meta=_meta(),
    )
