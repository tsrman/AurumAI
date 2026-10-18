import uuid
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone

from src.core.dependencies import get_service_db_session
from src.core.auth import get_current_user_or_session, UserInfo
from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    ResourceRef,
    ResourceType,
)
from src.core.capability_catalog import Capability
from src.models.skill import Skill
from src.schemas.skill import SkillResponse
from src.schemas.common import ApiResponse, Meta
from src.services.authorization import authorization_service
from src.services.skill_registry import skill_registry

router = APIRouter()


def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _is_internal_service(user: UserInfo) -> bool:
    """True for runner/tool-worker session tokens (not browser JWTs)."""
    return user.raw_claims.get("type") == "session"


def _authorization_subject(user: UserInfo) -> AuthorizationSubject:
    try:
        return AuthorizationSubject(
            user_id=UUID(user.user_id),
            org_id=UUID(user.org_id or ""),
            role=user.role,
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing or invalid organization context",
        )


async def _require_skill_create(
    user: UserInfo,
    session: AsyncSession,
) -> AuthorizationSubject:
    subject = _authorization_subject(user)
    if not await authorization_service.has_capability(
        session,
        subject,
        Capability.WORKSPACE_SKILLS_CREATE,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing capability: workspace.skills.create",
        )
    return subject


async def _require_skill_write(
    user: UserInfo,
    session: AsyncSession,
    skill: Skill,
) -> AuthorizationSubject:
    subject = _authorization_subject(user)
    if skill.org_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Global skills are managed by the platform operator",
        )
    resource = ResourceRef(ResourceType.SKILL, str(skill.id))
    if not await authorization_service.can_access(
        session,
        subject,
        resource,
        AccessPermission.READ,
        resource_org_id=skill.org_id,
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    if not await authorization_service.can_access(
        session,
        subject,
        resource,
        AccessPermission.WRITE,
        resource_org_id=skill.org_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Skill write access required",
        )
    return subject


def _skill_response(skill: Skill, include_secrets: bool = False) -> SkillResponse:
    """`mcp_auth_token` is a write-only secret for browser clients: only
    internal session-token callers (tool-worker needs it to call the MCP
    server) get it back — a regular org user must not be able to read
    admin-configured credentials out of GET /v1/skills."""
    return SkillResponse(
        id=str(skill.id),
        orgId=str(skill.org_id) if skill.org_id else None,
        name=skill.name,
        description=skill.description,
        type=skill.type,
        workflowType=skill.workflow_type,
        activityName=skill.activity_name,
        mcpTransport=skill.mcp_transport,
        mcpEndpoint=skill.mcp_endpoint,
        mcpAuthToken=skill.mcp_auth_token if include_secrets else None,
        hasMcpAuthToken=bool(skill.mcp_auth_token),
        inputSchema=skill.input_schema if isinstance(skill.input_schema, dict) else {},
        allowedChildSkills=skill.allowed_child_skills
        if isinstance(skill.allowed_child_skills, list)
        else None,
        bundleSkillName=skill.bundle_skill_name,
        bundleSizeBytes=skill.bundle_size_bytes,
        bundleChecksum=skill.bundle_checksum,
        bundleStorageKey=skill.bundle_storage_key,
        networkPolicy=skill.network_policy or "none",
        createdAt=skill.created_at.isoformat(),
        updatedAt=skill.updated_at.isoformat(),
    )


@router.get("/")
async def list_skills(
    org_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    # Only ever meaningful together with a session token scoped to the same
    # conversation (get_current_user_or_session 403s any mismatch before this
    # handler runs) — surfaces that one conversation's hidden
    # execute_hybrid_search skill (chat file attachments) alongside the
    # ordinary ones. Browser callers never pass this and see only the latter.
    conversation_id: Optional[str] = None,
    # get_current_user_or_session: the conversation-runner's
    # list_available_skills activity calls this endpoint with its internal
    # session token, not a Keycloak JWT.
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> ApiResponse[list[SkillResponse]]:
    del org_id
    subject = _authorization_subject(user)
    try:
        conversation_uuid = UUID(conversation_id) if conversation_id else None
    except ValueError:
        conversation_uuid = None
    skills = await skill_registry.list_accessible_skills(
        session,
        subject,
        limit,
        offset,
        conversation_id=conversation_uuid,
    )
    include_secrets = _is_internal_service(user)
    return ApiResponse(
        data=[_skill_response(s, include_secrets=include_secrets) for s in skills],
        meta=_meta(),
    )


@router.get("/{skill_id}")
async def get_skill(
    skill_id: str,
    # get_current_user_or_session: tool-worker's execute_skill activity
    # fetches skill definitions with the calling conversation's session token.
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> ApiResponse[SkillResponse]:
    skill = await skill_registry.get_skill(session, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    subject = _authorization_subject(user)
    if not await authorization_service.can_access(
        session,
        subject,
        ResourceRef(ResourceType.SKILL, str(skill.id)),
        AccessPermission.READ,
        resource_org_id=skill.org_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Skill not found",
        )
    return ApiResponse(
        data=_skill_response(skill, include_secrets=_is_internal_service(user)),
        meta=_meta(),
    )
