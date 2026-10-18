import uuid
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from src.core.auth import UserInfo, get_current_user
from src.core.authorization_types import (
    AccessPermission,
    AuthorizationSubject,
    ResourceType,
)
from src.core.capability_catalog import Capability
from src.core.dependencies import get_org_db_session
from src.models.knowledge_base import KnowledgeBase
from src.repositories.knowledge_base_repository import knowledge_base_repository
from src.schemas.common import ApiResponse, Meta
from src.schemas.knowledge_base import KnowledgeBaseCreate, KnowledgeBaseResponse
from src.services.authorization import authorization_service
from src.services.rag_client import rag_client

router = APIRouter(prefix="/knowledge-bases")


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


def _response(
    knowledge_base: KnowledgeBase, *, can_write: bool
) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse(
        id=knowledge_base.id,
        name=knowledge_base.name,
        description=knowledge_base.description,
        canWrite=can_write,
        createdAt=knowledge_base.created_at,
        updatedAt=knowledge_base.updated_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    data: KnowledgeBaseCreate,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[KnowledgeBaseResponse]:
    subject = _subject(user)
    if not await authorization_service.has_capability(
        session,
        subject,
        Capability.WORKSPACE_KNOWLEDGE_CREATE,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing capability: workspace.knowledge.create",
        )
    knowledge_base = await knowledge_base_repository.create_with_owner_write_grant(
        session,
        subject,
        name=data.name,
        description=data.description,
    )
    return ApiResponse(data=_response(knowledge_base, can_write=True), meta=_meta())


@router.get("")
async def list_knowledge_bases(
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[list[KnowledgeBaseResponse]]:
    subject = _subject(user)
    rows = await knowledge_base_repository.list_accessible(session, subject)
    writable_ids = await authorization_service.accessible_resource_ids(
        session,
        subject,
        ResourceType.KNOWLEDGE_BASE,
        AccessPermission.WRITE,
        [str(row.id) for row in rows],
    )
    return ApiResponse(
        data=[
            _response(row, can_write=str(row.id) in writable_ids) for row in rows
        ],
        meta=_meta(),
    )


@router.get("/{knowledge_base_id}")
async def get_knowledge_base(
    knowledge_base_id: UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[KnowledgeBaseResponse]:
    subject = _subject(user)
    knowledge_base = await knowledge_base_repository.get_accessible(
        session,
        subject,
        knowledge_base_id,
    )
    writable = (
        await knowledge_base_repository.get_accessible(
            session,
            subject,
            knowledge_base_id,
            AccessPermission.WRITE,
        )
        is not None
    )
    if knowledge_base is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Knowledge base not found",
        )
    return ApiResponse(
        data=_response(knowledge_base, can_write=writable), meta=_meta()
    )


@router.delete("/{knowledge_base_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    knowledge_base_id: UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> None:
    subject = _subject(user)
    from src.models.archive_control import ConversationOperation
    from src.services.conversation_lifecycle import lock_active_conversation

    owner = await session.scalar(select(KnowledgeBase.owning_conversation_id).where(
        KnowledgeBase.id == knowledge_base_id, KnowledgeBase.org_id == subject.org_id
    ))
    if owner is not None:
        identity = await lock_active_conversation(session, subject.org_id, owner)
        session.info["conversation_fence"] = (subject.org_id, owner, identity.generation)
    await knowledge_base_repository.lock_for_write(session, knowledge_base_id)
    visible = await knowledge_base_repository.get_accessible(
        session,
        subject,
        knowledge_base_id,
        AccessPermission.READ,
    )
    if visible is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Knowledge base not found",
        )
    knowledge_base = await knowledge_base_repository.get_accessible(
        session,
        subject,
        knowledge_base_id,
        AccessPermission.WRITE,
    )
    if knowledge_base is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Knowledge base write access required",
        )

    if await session.scalar(select(ConversationOperation.id).where(
        ConversationOperation.org_id == subject.org_id,
        ConversationOperation.knowledge_base_id == knowledge_base_id,
        ConversationOperation.status != "settled",
    ).limit(1)) is not None:
        raise HTTPException(409, "Knowledge base has an unsettled document operation")

    documents = await rag_client.list_documents(
        str(subject.org_id), str(knowledge_base_id)
    )
    if documents:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Knowledge base contains documents",
        )
    await knowledge_base_repository.delete_with_grants(session, knowledge_base)
