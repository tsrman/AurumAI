import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo
from src.core.authorization_dependencies import require_org_admin
from src.core.dependencies import get_current_org_id, get_org_db_session
from src.schemas.admin_models import (
    AdminModelCatalogResponse,
    AdminModelEntryResponse,
    OrganizationImageModelsResponse,
    OrganizationImageModelsUpdate,
    OrganizationModelSettingResponse,
    OrganizationModelSettingsUpdate,
)
from src.core.config import settings
from src.schemas.common import ApiResponse, Meta
from src.services.model_catalog import (
    GatewayCatalogSnapshot,
    gateway_catalog_snapshot,
)
from src.services.organization_model_settings import (
    GatewayUnavailable,
    ModelNotFound,
    ModelNotOrganizationReadable,
    OrganizationModelView,
    OrganizationModelSettingsService,
    organization_model_settings_service,
)


router = APIRouter()
GatewayCatalogLoader = Callable[[], Awaitable[GatewayCatalogSnapshot]]


def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def get_gateway_catalog_loader() -> GatewayCatalogLoader:
    return gateway_catalog_snapshot


def get_organization_model_settings_service() -> OrganizationModelSettingsService:
    return organization_model_settings_service


def _catalog_response(view: OrganizationModelView) -> AdminModelCatalogResponse:
    return AdminModelCatalogResponse(
        gateway_status=view.gateway_status,
        default_model=view.default_model,
        default_available=view.default_available,
        default_temperature=view.default_temperature,
        models=[
            AdminModelEntryResponse(
                id=entry.id,
                organization_readable=entry.organization_readable,
                is_default=entry.is_default,
            )
            for entry in view.models
        ],
    )


@router.get("/models", response_model=ApiResponse[AdminModelCatalogResponse])
async def admin_models(
    user: UserInfo = Depends(require_org_admin),
    org_id: UUID = Depends(get_current_org_id),
    session: AsyncSession = Depends(get_org_db_session),
    load_catalog: GatewayCatalogLoader = Depends(get_gateway_catalog_loader),
    service: OrganizationModelSettingsService = Depends(
        get_organization_model_settings_service
    ),
) -> ApiResponse[AdminModelCatalogResponse]:
    snapshot = await load_catalog()
    view = await service.inspect(session, org_id, snapshot)
    return ApiResponse(data=_catalog_response(view), meta=_meta())


@router.put(
    "/settings/models",
    response_model=ApiResponse[OrganizationModelSettingResponse],
)
async def update_organization_model_settings(
    data: OrganizationModelSettingsUpdate,
    user: UserInfo = Depends(require_org_admin),
    org_id: UUID = Depends(get_current_org_id),
    session: AsyncSession = Depends(get_org_db_session),
    load_catalog: GatewayCatalogLoader = Depends(get_gateway_catalog_loader),
    service: OrganizationModelSettingsService = Depends(
        get_organization_model_settings_service
    ),
) -> ApiResponse[OrganizationModelSettingResponse]:
    snapshot = await load_catalog() if data.default_model is not None else None
    try:
        await service.set_default(
            session,
            org_id,
            data.default_model,
            snapshot,
        )
    except GatewayUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Model gateway unavailable",
        ) from exc
    except ModelNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Model is not available for chat",
        ) from exc
    except ModelNotOrganizationReadable as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Model requires an organization grant",
        ) from exc
    await service.set_default_temperature(session, org_id, data.default_temperature)
    await session.commit()
    return ApiResponse(
        data=OrganizationModelSettingResponse(
            default_model=data.default_model,
            default_temperature=data.default_temperature,
        ),
        meta=_meta(),
    )


@router.get("/settings/images", response_model=ApiResponse[OrganizationImageModelsResponse])
async def organization_image_models(
    user: UserInfo = Depends(require_org_admin),
    org_id: UUID = Depends(get_current_org_id),
    session: AsyncSession = Depends(get_org_db_session),
    load_catalog: GatewayCatalogLoader = Depends(get_gateway_catalog_loader),
    service: OrganizationModelSettingsService = Depends(
        get_organization_model_settings_service
    ),
) -> ApiResponse[OrganizationImageModelsResponse]:
    snapshot = await load_catalog()
    generation_model, edit_model = await service.image_models(session, org_id)
    return ApiResponse(
        data=OrganizationImageModelsResponse(
            enabled=settings.IMAGES_ENABLED,
            gateway_status=snapshot.status,
            generation_model=generation_model,
            edit_model=edit_model,
            models=list(service.catalog_model_ids(snapshot)),
        ),
        meta=_meta(),
    )


@router.put("/settings/images", response_model=ApiResponse[OrganizationImageModelsResponse])
async def update_organization_image_models(
    data: OrganizationImageModelsUpdate,
    user: UserInfo = Depends(require_org_admin),
    org_id: UUID = Depends(get_current_org_id),
    session: AsyncSession = Depends(get_org_db_session),
    load_catalog: GatewayCatalogLoader = Depends(get_gateway_catalog_loader),
    service: OrganizationModelSettingsService = Depends(
        get_organization_model_settings_service
    ),
) -> ApiResponse[OrganizationImageModelsResponse]:
    snapshot = await load_catalog()
    try:
        await service.set_image_models(
            session, org_id, data.generation_model, data.edit_model, snapshot
        )
    except GatewayUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Model gateway unavailable",
        ) from exc
    except ModelNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Model is not published by the gateway",
        ) from exc
    await session.commit()
    return ApiResponse(
        data=OrganizationImageModelsResponse(
            enabled=settings.IMAGES_ENABLED,
            gateway_status=snapshot.status,
            generation_model=data.generation_model,
            edit_model=data.edit_model,
            models=list(service.catalog_model_ids(snapshot)),
        ),
        meta=_meta(),
    )
