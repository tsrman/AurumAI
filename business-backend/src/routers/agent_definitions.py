from typing import Literal, cast
import uuid
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user, get_current_user_or_session
from src.core.authorization_types import AccessPermission, AuthorizationSubject, ResourceType
from src.core.capability_catalog import Capability
from src.core.dependencies import get_org_db_session, get_service_db_session
from src.models.agent_definition import AgentDefinition
from src.repositories.agent_definition_repository import agent_definition_repository
from src.schemas.agent_definition import (
    AgentDefinitionCreate,
    AgentDefinitionResponse,
    AgentDefinitionUpdate,
)
from src.schemas.common import ApiResponse, Meta
from src.services.agent_authorization import (
    AgentAuthorizationService,
    AgentDefinitionNotFound,
    AgentDependencyAccessDenied,
    agent_authorization_service,
)
from src.services.authorization import authorization_service
from src.repositories.workflow_definition_repository import workflow_definition_repository


router = APIRouter(prefix="/agent-definitions")


def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def _subject(user: UserInfo) -> AuthorizationSubject:
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


def _response(agent: AgentDefinition, *, can_write: bool) -> AgentDefinitionResponse:
    return AgentDefinitionResponse(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        modelId=agent.model_id,
        systemPrompt=agent.system_prompt,
        temperature=agent.temperature,
        skillIds=agent.skill_ids,
        knowledgeBaseIds=agent.knowledge_base_ids,
        kind=cast(Literal["agent", "hermes"], agent.kind),
        canWrite=can_write,
        createdAt=agent.created_at,
        updatedAt=agent.updated_at,
    )


async def _require_dependency_access(
    service: AgentAuthorizationService,
    session: AsyncSession,
    subject: AuthorizationSubject,
    *,
    agent_id: UUID,
    model_id: str,
    skill_ids: list[UUID],
    knowledge_base_ids: list[UUID],
) -> None:
    try:
        await service.require_dependencies(
            session,
            subject,
            agent_id=agent_id,
            model_id=model_id,
            skill_ids=skill_ids,
            knowledge_base_ids=knowledge_base_ids,
        )
    except AgentDependencyAccessDenied:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent dependency not found or inaccessible",
        )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent_definition(
    data: AgentDefinitionCreate,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[AgentDefinitionResponse]:
    subject = _subject(user)
    if not await authorization_service.has_capability(
        session, subject, Capability.WORKSPACE_AGENTS_CREATE
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing capability: workspace.agents.create",
        )

    await _require_dependency_access(
        agent_authorization_service,
        session,
        subject,
        agent_id=uuid.uuid4(),
        model_id=data.model_id,
        skill_ids=data.skill_ids,
        knowledge_base_ids=data.knowledge_base_ids,
    )
    try:
        agent = await agent_definition_repository.create_with_owner_write_grant(
            session, subject, data
        )
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Agent definition name already exists",
        )
    return ApiResponse(data=_response(agent, can_write=True), meta=_meta())


@router.get("")
async def list_agent_definitions(
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[list[AgentDefinitionResponse]]:
    subject = _subject(user)
    rows = await agent_definition_repository.list_accessible(session, subject)
    writable_ids = await authorization_service.accessible_resource_ids(
        session,
        subject,
        ResourceType.AGENT_DEFINITION,
        AccessPermission.WRITE,
        [str(row.id) for row in rows],
    )
    return ApiResponse(
        data=[
            _response(row, can_write=str(row.id) in writable_ids) for row in rows
        ],
        meta=_meta(),
    )


@router.get("/{agent_id}")
async def get_agent_definition(
    agent_id: UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[AgentDefinitionResponse]:
    subject = _subject(user)
    agent = await agent_definition_repository.get_accessible(
        session, subject, agent_id, AccessPermission.READ
    )
    writable = (
        await agent_definition_repository.get_accessible(
            session, subject, agent_id, AccessPermission.WRITE
        )
        is not None
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent definition not found",
        )
    return ApiResponse(data=_response(agent, can_write=writable), meta=_meta())


@router.patch("/{agent_id}")
async def update_agent_definition(
    agent_id: UUID,
    data: AgentDefinitionUpdate,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[AgentDefinitionResponse]:
    subject = _subject(user)
    visible = await agent_definition_repository.get_accessible(
        session, subject, agent_id, AccessPermission.READ
    )
    if visible is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent definition not found",
        )
    agent = await agent_definition_repository.get_accessible(
        session, subject, agent_id, AccessPermission.WRITE
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent definition write access required",
        )

    model_id = data.model_id if "model_id" in data.model_fields_set else agent.model_id
    skill_ids = data.skill_ids if "skill_ids" in data.model_fields_set else agent.skill_ids
    knowledge_base_ids = (
        data.knowledge_base_ids
        if "knowledge_base_ids" in data.model_fields_set
        else agent.knowledge_base_ids
    )
    # Explicit nulls are rejected by AgentDefinitionUpdate validators; these
    # assertions document the narrowed post-validation types for mypy.
    assert model_id is not None
    assert skill_ids is not None
    assert knowledge_base_ids is not None
    if agent.kind == "hermes" and (skill_ids or knowledge_base_ids):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A Hermes Agent definition takes no skills or knowledge bases",
        )
    await _require_dependency_access(
        agent_authorization_service,
        session,
        subject,
        agent_id=agent.id,
        model_id=model_id,
        skill_ids=skill_ids,
        knowledge_base_ids=knowledge_base_ids,
    )
    try:
        updated = await agent_definition_repository.update(session, agent, data)
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Agent definition name already exists",
        )
    return ApiResponse(data=_response(updated, can_write=True), meta=_meta())


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_definition(
    agent_id: UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> None:
    subject = _subject(user)
    visible = await agent_definition_repository.get_accessible(
        session, subject, agent_id, AccessPermission.READ
    )
    if visible is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent definition not found",
        )
    agent = await agent_definition_repository.get_accessible(
        session, subject, agent_id, AccessPermission.WRITE
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent definition write access required",
        )
    if await workflow_definition_repository.is_agent_referenced(
        session, subject.org_id, agent.id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Agent definition is used by a workflow",
        )
    await agent_definition_repository.delete_with_grants(session, agent)


@router.get("/{agent_id}/execution")
async def resolve_agent_execution(
    agent_id: UUID,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> ApiResponse[AgentDefinitionResponse]:
    subject = _subject(user)
    try:
        agent = await agent_authorization_service.require_executable(
            session, subject, agent_id
        )
    except AgentDefinitionNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent definition not found",
        )
    except AgentDependencyAccessDenied:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent dependencies are not accessible",
        )
    writable = (
        await agent_definition_repository.get_accessible(
            session, subject, agent_id, AccessPermission.WRITE
        )
        is not None
    )
    return ApiResponse(data=_response(agent, can_write=writable), meta=_meta())
