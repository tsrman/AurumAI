import uuid
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    PrincipalType,
    ResourceRef,
    ResourceType,
)
from src.core.capability_catalog import CAPABILITY_CATALOG
from src.core.dependencies import get_org_db_session
from src.models.access_grant import AccessGrant
from src.models.group import Group
from src.models.organization import Organization
from src.models.user import User
from src.schemas.authorization import (
    AccessPreviewRequest,
    AccessPreviewResponse,
    AuthorizationPrincipalResponse,
    CapabilityAssignmentRequest,
    CapabilityAssignmentResponse,
    CapabilityResponse,
    GrantCreateRequest,
    GrantReconcileRequest,
    GrantResponse,
    PreviewResourceResponse,
)
from src.schemas.common import ApiResponse, Meta
from src.services.grant_administration import (
    AccessPreview,
    GrantAdministrationService,
    GrantSpec,
    GrantTargetNotFound,
    GrantWriteDenied,
    grant_administration_service,
)

router = APIRouter()


def _meta(request_id: str | None = None) -> Meta:
    return Meta(
        requestId=request_id or f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _subject(user: UserInfo = Depends(get_current_user)) -> AuthorizationSubject:
    if user.role != "org_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    try:
        return AuthorizationSubject(
            user_id=UUID(user.user_id),
            org_id=UUID(user.org_id or ""),
            role=user.role,
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing or invalid organization context",
        )


def _response(row: AccessGrant) -> GrantResponse:
    return GrantResponse(
        id=row.id,
        resource_type=ResourceType(row.resource_type),
        resource_id=row.resource_id,
        principal_type=PrincipalType(row.principal_type),
        principal_id=row.principal_id,
        permission=AccessPermission(row.permission),
        created_by=row.created_by,
        created_via=row.created_via,
    )


def _correlation_id(request: Request) -> str:
    return request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex}"


def _resource_ref(resource_type: ResourceType, resource_id: str) -> ResourceRef:
    try:
        return ResourceRef(resource_type, resource_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )


def _preview_response(preview: AccessPreview) -> AccessPreviewResponse:
    return AccessPreviewResponse(
        subject_type=preview.subject_type,
        subject_id=preview.subject_id,
        admin_bypass=preview.admin_bypass,
        capabilities=list(preview.capabilities),
        resources=[
            PreviewResourceResponse(
                resource_type=item.resource.resource_type,
                resource_id=item.resource.resource_id,
                can_read=item.can_read,
                can_write=item.can_write,
                source=item.source,
            )
            for item in preview.resources
        ],
    )


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, GrantWriteDenied):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grant target not found")


@router.get("/capabilities")
async def capability_catalog(
    _actor: AuthorizationSubject = Depends(_subject),
) -> ApiResponse[list[CapabilityResponse]]:
    return ApiResponse(
        data=[
            CapabilityResponse(
                key=definition.key.value,
                label=definition.label,
                description=definition.description,
            )
            for definition in CAPABILITY_CATALOG.definitions()
        ],
        meta=_meta(),
    )


@router.get("/subjects")
async def authorization_subjects(
    actor: AuthorizationSubject = Depends(_subject),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[list[AuthorizationPrincipalResponse]]:
    organization = (
        await session.execute(
            select(Organization).where(Organization.id == actor.org_id)
        )
    ).scalar_one()
    groups = list(
        (
            await session.execute(
                select(Group)
                .where(Group.org_id == actor.org_id)
                .order_by(Group.name, Group.id)
            )
        ).scalars()
    )
    users = list(
        (
            await session.execute(
                select(User)
                .where(User.org_id == actor.org_id)
                .order_by(User.full_name, User.email, User.id)
            )
        ).scalars()
    )
    data = [
        AuthorizationPrincipalResponse(
            principal_type=PrincipalType.ORGANIZATION,
            principal_id=organization.id,
            label=organization.name,
        ),
        *(
            AuthorizationPrincipalResponse(
                principal_type=PrincipalType.GROUP,
                principal_id=group.id,
                label=group.name,
            )
            for group in groups
        ),
        *(
            AuthorizationPrincipalResponse(
                principal_type=PrincipalType.USER,
                principal_id=user.id,
                label=user.full_name or user.email,
            )
            for user in users
        ),
    ]
    return ApiResponse(data=data, meta=_meta())


@router.get("/capabilities/organization")
async def get_organization_capabilities(
    actor: AuthorizationSubject = Depends(_subject),
    session: AsyncSession = Depends(get_org_db_session),
    service: GrantAdministrationService = Depends(lambda: grant_administration_service),
) -> ApiResponse[CapabilityAssignmentResponse]:
    capabilities = await service.organization_capabilities(session, actor)
    return ApiResponse(
        data=CapabilityAssignmentResponse(capabilities=list(capabilities)), meta=_meta()
    )


@router.put("/capabilities/organization")
async def put_organization_capabilities(
    data: CapabilityAssignmentRequest,
    actor: AuthorizationSubject = Depends(_subject),
    session: AsyncSession = Depends(get_org_db_session),
    service: GrantAdministrationService = Depends(lambda: grant_administration_service),
) -> ApiResponse[CapabilityAssignmentResponse]:
    capabilities = await service.reconcile_organization_capabilities(
        session, actor, data.capabilities
    )
    await session.commit()
    return ApiResponse(
        data=CapabilityAssignmentResponse(capabilities=list(capabilities)), meta=_meta()
    )


@router.get("/capabilities/groups/{group_id}")
async def get_group_capabilities(
    group_id: UUID,
    actor: AuthorizationSubject = Depends(_subject),
    session: AsyncSession = Depends(get_org_db_session),
    service: GrantAdministrationService = Depends(lambda: grant_administration_service),
) -> ApiResponse[CapabilityAssignmentResponse]:
    try:
        capabilities = await service.group_capabilities(session, actor, group_id)
    except GrantTargetNotFound as exc:
        raise _translate_error(exc)
    return ApiResponse(
        data=CapabilityAssignmentResponse(capabilities=list(capabilities)), meta=_meta()
    )


@router.put("/capabilities/groups/{group_id}")
async def put_group_capabilities(
    group_id: UUID,
    data: CapabilityAssignmentRequest,
    actor: AuthorizationSubject = Depends(_subject),
    session: AsyncSession = Depends(get_org_db_session),
    service: GrantAdministrationService = Depends(lambda: grant_administration_service),
) -> ApiResponse[CapabilityAssignmentResponse]:
    try:
        capabilities = await service.reconcile_group_capabilities(
            session, actor, group_id, data.capabilities
        )
        await session.commit()
    except GrantTargetNotFound as exc:
        raise _translate_error(exc)
    return ApiResponse(
        data=CapabilityAssignmentResponse(capabilities=list(capabilities)), meta=_meta()
    )


@router.post("/preview/users/{user_id}")
async def preview_user(
    user_id: UUID,
    data: AccessPreviewRequest,
    actor: AuthorizationSubject = Depends(_subject),
    session: AsyncSession = Depends(get_org_db_session),
    service: GrantAdministrationService = Depends(lambda: grant_administration_service),
) -> ApiResponse[AccessPreviewResponse]:
    try:
        preview = await service.preview_user(
            session,
            actor,
            user_id,
            tuple(
                ResourceRef(item.resource_type, item.resource_id)
                for item in data.resources
            ),
        )
    except GrantTargetNotFound as exc:
        raise _translate_error(exc)
    return ApiResponse(data=_preview_response(preview), meta=_meta())


@router.post("/preview/groups/{group_id}")
async def preview_group(
    group_id: UUID,
    data: AccessPreviewRequest,
    actor: AuthorizationSubject = Depends(_subject),
    session: AsyncSession = Depends(get_org_db_session),
    service: GrantAdministrationService = Depends(lambda: grant_administration_service),
) -> ApiResponse[AccessPreviewResponse]:
    try:
        preview = await service.preview_group(
            session,
            actor,
            group_id,
            tuple(
                ResourceRef(item.resource_type, item.resource_id)
                for item in data.resources
            ),
        )
    except GrantTargetNotFound as exc:
        raise _translate_error(exc)
    return ApiResponse(data=_preview_response(preview), meta=_meta())


@router.get("/grants")
async def list_grants(
    resource_type: ResourceType = Query(alias="resourceType"),
    resource_id: str = Query(alias="resourceId", min_length=1, max_length=255),
    actor: AuthorizationSubject = Depends(_subject),
    session: AsyncSession = Depends(get_org_db_session),
    service: GrantAdministrationService = Depends(lambda: grant_administration_service),
) -> ApiResponse[list[GrantResponse]]:
    try:
        rows = await service.list_for_resource(
            session, actor, _resource_ref(resource_type, resource_id)
        )
    except (GrantWriteDenied, GrantTargetNotFound) as exc:
        raise _translate_error(exc)
    return ApiResponse(data=[_response(row) for row in rows], meta=_meta())


@router.post("/grants", status_code=status.HTTP_201_CREATED)
async def create_grant(
    data: GrantCreateRequest,
    request: Request,
    actor: AuthorizationSubject = Depends(_subject),
    session: AsyncSession = Depends(get_org_db_session),
    service: GrantAdministrationService = Depends(lambda: grant_administration_service),
) -> ApiResponse[GrantResponse]:
    correlation_id = _correlation_id(request)
    try:
        row = await service.create(
            session,
            actor,
            ResourceRef(data.resource_type, data.resource_id),
            GrantSpec(data.principal_type, data.principal_id, data.permission),
            correlation_id=correlation_id,
        )
        await session.commit()
    except (GrantWriteDenied, GrantTargetNotFound) as exc:
        raise _translate_error(exc)
    return ApiResponse(data=_response(row), meta=_meta(correlation_id))


@router.put("/grants/reconcile")
async def reconcile_grants(
    data: GrantReconcileRequest,
    request: Request,
    actor: AuthorizationSubject = Depends(_subject),
    session: AsyncSession = Depends(get_org_db_session),
    service: GrantAdministrationService = Depends(lambda: grant_administration_service),
) -> ApiResponse[list[GrantResponse]]:
    correlation_id = _correlation_id(request)
    try:
        rows = await service.reconcile(
            session,
            actor,
            ResourceRef(data.resource_type, data.resource_id),
            tuple(
                GrantSpec(item.principal_type, item.principal_id, item.permission)
                for item in data.grants
            ),
            correlation_id=correlation_id,
        )
        await session.commit()
    except (GrantWriteDenied, GrantTargetNotFound) as exc:
        raise _translate_error(exc)
    return ApiResponse(data=[_response(row) for row in rows], meta=_meta(correlation_id))


@router.delete("/grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_grant(
    grant_id: UUID,
    request: Request,
    actor: AuthorizationSubject = Depends(_subject),
    session: AsyncSession = Depends(get_org_db_session),
    service: GrantAdministrationService = Depends(lambda: grant_administration_service),
) -> None:
    try:
        await service.delete(
            session,
            actor,
            grant_id,
            correlation_id=_correlation_id(request),
        )
        await session.commit()
    except (GrantWriteDenied, GrantTargetNotFound) as exc:
        raise _translate_error(exc)
