import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user, get_current_user_or_session
from src.core.authorization_types import AccessPermission, AuthorizationSubject
from src.core.dependencies import get_org_db_session, get_service_db_session
from src.models.knowledge_base import KnowledgeBase
from src.repositories.knowledge_base_repository import knowledge_base_repository
from src.schemas.common import ApiResponse, Meta
from src.schemas.document import DocumentCreate, DocumentResponse, DocumentSearch
from src.services.document_cleanup import document_cleanup_service
from src.services.document_upload import store_and_index_document
from src.services.rag_client import rag_client

router = APIRouter()
SAFE_DEGRADED_SEARCH_WARNING = "Search completed with limitations"


def _subject(user: UserInfo) -> AuthorizationSubject:
    try:
        return AuthorizationSubject(
            user_id=uuid.UUID(user.user_id),
            org_id=uuid.UUID(user.org_id or ""),
            role=user.role,
        )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing or invalid organization context",
        )


async def _require_knowledge_access(
    user: UserInfo,
    session: AsyncSession,
    knowledge_base_id: uuid.UUID,
    permission: AccessPermission,
) -> KnowledgeBase:
    subject = _subject(user)
    from sqlalchemy import select
    from src.services.conversation_lifecycle import ConversationFrozen, lock_active_conversation
    owner = await session.scalar(select(KnowledgeBase.owning_conversation_id).where(KnowledgeBase.id == knowledge_base_id, KnowledgeBase.org_id == subject.org_id))
    if owner is not None:
        try:
            identity = await lock_active_conversation(session, subject.org_id, owner)
        except ConversationFrozen:
            if permission is AccessPermission.READ:
                raise HTTPException(404, "Knowledge base not found") from None
            raise
        session.info["conversation_fence"] = (subject.org_id, owner, identity.generation)
    if permission is AccessPermission.WRITE:
        await knowledge_base_repository.lock_for_write(session, knowledge_base_id)
    knowledge_base = await knowledge_base_repository.get_accessible(
        session, subject, knowledge_base_id, AccessPermission.READ
    )
    if knowledge_base is not None and permission is AccessPermission.WRITE:
        writable = await knowledge_base_repository.get_accessible(
            session, subject, knowledge_base_id, AccessPermission.WRITE
        )
        if writable is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Knowledge base write access required",
            )
        knowledge_base = writable
    if knowledge_base is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Knowledge base not found",
        )
    return knowledge_base

def _meta() -> Meta:
    return Meta(
        requestId=f"req_{uuid.uuid4().hex}",
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

def _parse_doc(data: dict[str, Any]) -> DocumentResponse:
    return DocumentResponse.model_validate(data)


def _safe_search_response(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "results": payload.get("results", []),
        "warnings": (
            [SAFE_DEGRADED_SEARCH_WARNING] if payload.get("warnings") else []
        ),
    }

@router.post("/documents", status_code=202)
async def create_document(
    data: DocumentCreate,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[DocumentResponse]:
    await _require_knowledge_access(
        user,
        session,
        data.knowledge_base_id,
        AccessPermission.WRITE,
    )
    from src.services.conversation_lifecycle import begin_attachment_operation, settle_operation
    operation = await begin_attachment_operation(session, _subject(user).org_id, data.knowledge_base_id, 'document_create', uuid.uuid4())
    doc = await rag_client.create_document(
        user.org_id, str(data.knowledge_base_id), data.title, data.content,
        **({"operation_id":str(operation.operation_key)} if operation else {}),
    )
    if operation is not None:
        await settle_operation(session, operation)
        await session.commit()
    return ApiResponse(data=_parse_doc(doc), meta=_meta())

@router.post("/documents/upload", status_code=202)
async def upload_document(
    knowledge_base_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[DocumentResponse]:
    """Upload a source file and create its document record.

    Use the API instead of a presigned MinIO link: its host port is loopback
    only, so remote browsers cannot reach it directly. Store the original
    bytes here; rag-worker performs Docling parsing later.
    """
    await _require_knowledge_access(
        user,
        session,
        knowledge_base_id,
        AccessPermission.WRITE,
    )
    doc = await store_and_index_document(
        session,
        org_id=user.org_id or "",
        knowledge_base_id=knowledge_base_id,
        file=file,
        uploaded_by=user.user_id,
    )
    return ApiResponse(data=_parse_doc(doc), meta=_meta())

@router.get("/documents")
async def list_documents(
    knowledge_base_id: uuid.UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[list[DocumentResponse]]:
    await _require_knowledge_access(
        user,
        session,
        knowledge_base_id,
        AccessPermission.READ,
    )
    docs = await rag_client.list_documents(user.org_id, str(knowledge_base_id))
    return ApiResponse(
        data=[_parse_doc(d) for d in docs],
        meta=_meta(),
    )

@router.get("/documents/{document_id}")
async def get_document(
    document_id: str,
    knowledge_base_id: uuid.UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> ApiResponse[DocumentResponse]:
    await _require_knowledge_access(
        user,
        session,
        knowledge_base_id,
        AccessPermission.READ,
    )
    doc = await rag_client.get_document(
        user.org_id, str(knowledge_base_id), document_id
    )
    return ApiResponse(data=_parse_doc(doc), meta=_meta())

@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: str,
    knowledge_base_id: uuid.UUID,
    user: UserInfo = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_db_session),
) -> None:
    await _require_knowledge_access(
        user,
        session,
        knowledge_base_id,
        AccessPermission.WRITE,
    )
    await document_cleanup_service.delete(
        session,
        org_id=_subject(user).org_id,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
    )


@router.post("/knowledge-bases/{knowledge_base_id}/search")
async def search_knowledge_base(
    knowledge_base_id: uuid.UUID,
    data: DocumentSearch,
    user: UserInfo = Depends(get_current_user_or_session),
    session: AsyncSession = Depends(get_service_db_session),
) -> dict[str, Any]:
    await _require_knowledge_access(
        user,
        session,
        knowledge_base_id,
        AccessPermission.READ,
    )
    return _safe_search_response(
        await rag_client.search(
            user.org_id,
            str(knowledge_base_id),
            query=data.query,
            limit=data.limit,
        )
    )
