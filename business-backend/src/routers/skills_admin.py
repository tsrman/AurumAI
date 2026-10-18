from typing import Any
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.dependencies import get_org_db_session
from src.core.auth import UserInfo, get_current_user
from src.routers.skills import (
    _meta,
    _require_skill_create,
    _require_skill_write,
    _skill_response,
)
from src.schemas.skill import (
    SkillCreate,
    SkillUpdate,
    CodeSkillUpdate,
    SkillResponse,
    McpDiscoverRequest,
    McpToolInfo,
)
from src.schemas.common import ApiResponse
from src.services.skill_registry import skill_registry
from src.services.mcp_discovery import discover_mcp_tools, McpDiscoveryError

router = APIRouter()


def _require_tenant_mcp_transport(skill_type: str | None, transport: str | None) -> None:
    # Stdio executes a command in the shared tool-worker. Only global skills
    # installed by the platform operator may use that trusted execution path.
    if skill_type == "mcp" and transport != "sse":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Organization MCP skills require SSE; stdio is operator-only",
        )


@router.post("/discover-mcp-tools")
async def discover_mcp_tools_endpoint(
    data: McpDiscoverRequest,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[list[McpToolInfo]]:
    if data.mcpTransport != "sse":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tool discovery is only supported for the sse transport",
        )
    auth_token = data.mcpAuthToken
    # a browser never gets the real token back (see
    # SkillResponse.hasMcpAuthToken), so re-discovering tools for an
    # existing MCP skill whose token the admin isn't retyping needs to
    # fall back to what's actually stored, not silently discover with no
    # auth at all.
    if not auth_token and data.skillId:
        skill = await skill_registry.get_skill(session, data.skillId)
        if skill is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
        await _require_skill_write(user, session, skill)
        auth_token = skill.mcp_auth_token
        if auth_token and data.mcpEndpoint != skill.mcp_endpoint:
            raise HTTPException(
                status_code=422,
                detail="Saved MCP credentials can only be used with the saved endpoint",
            )
    elif data.skillId:
        skill = await skill_registry.get_skill(session, data.skillId)
        if skill is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
        await _require_skill_write(user, session, skill)
    else:
        await _require_skill_create(user, session)
    try:
        tools = await discover_mcp_tools(data.mcpEndpoint, auth_token=auth_token)
    except McpDiscoveryError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return ApiResponse(data=[McpToolInfo(**t) for t in tools], meta=_meta())


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_skill(
    data: SkillCreate,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[SkillResponse]:
    subject = await _require_skill_create(user, session)
    _require_tenant_mcp_transport(data.type, data.mcp_transport)
    create_data = data.model_dump()
    if user.org_id:
        create_data["org_id"] = user.org_id
    skill = await skill_registry.create_owned_skill(session, subject, **create_data)
    return ApiResponse(data=_skill_response(skill), meta=_meta())


@router.patch("/{skill_id}")
async def update_skill(
    skill_id: str,
    data: dict[str, Any],
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[SkillResponse]:
    skill = await skill_registry.get_skill(session, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    await _require_skill_write(user, session, skill)
    # code skills carry a bundle in object storage — only name/description
    # are patchable here; replacing the bundle goes through the dedicated
    # PUT /v1/skills/code/{id}/bundle endpoint instead.
    try:
        parsed: SkillUpdate | CodeSkillUpdate
        if skill.type == "code":
            parsed = CodeSkillUpdate(**data)
        else:
            parsed = SkillUpdate(**data)
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    update_data = parsed.model_dump(exclude_unset=True)
    _require_tenant_mcp_transport(
        update_data.get("type", skill.type),
        update_data.get("mcp_transport", skill.mcp_transport),
    )
    if "mcp_auth_token" not in update_data and any(
        key in update_data and update_data[key] != getattr(skill, key)
        for key in ("mcp_endpoint", "mcp_transport")
    ):
        # Endpoint edits must not silently carry a write-only credential to
        # another server. An explicitly supplied replacement is independent.
        update_data["mcp_auth_token"] = None
    skill = await skill_registry.update_skill(session, skill, **update_data)
    return ApiResponse(data=_skill_response(skill), meta=_meta())


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(
    skill_id: str,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> None:
    skill = await skill_registry.get_skill(session, skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
    subject = await _require_skill_write(user, session, skill)
    await skill_registry.delete_skill_with_grants(session, subject, skill)
