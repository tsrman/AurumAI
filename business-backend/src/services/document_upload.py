import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models.file_metadata import FileMetadata
from src.services.object_storage import object_storage
from src.services.rag_client import rag_client


async def store_and_index_document(
    session: AsyncSession,
    *,
    org_id: str,
    knowledge_base_id: uuid.UUID,
    file: UploadFile,
    uploaded_by: str | None,
) -> dict[str, Any]:
    """Validate, store in MinIO, index via rag-api, and record FileMetadata.

    Shared by the knowledge-base page's upload endpoint
    (routers/documents.py::upload_document) and the chat attachment endpoint
    (routers/chat.py) — everything below the "who may write to this
    knowledge base" check (each caller does its own) is identical between
    the two.
    """
    filename = Path(file.filename or "").name
    suffix = Path(filename).suffix.lower()
    if not filename or suffix not in settings.DOCUMENT_UPLOAD_ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                "Supported files: "
                + ", ".join(settings.DOCUMENT_UPLOAD_ALLOWED_SUFFIXES)
            ),
        )

    # Read the bounded file before storage: 25MB fits in memory, whereas
    # multipart S3 streaming would add complexity with little benefit at
    # this upload limit.
    payload = await file.read(settings.DOCUMENT_UPLOAD_MAX_BYTES + 1)
    if len(payload) > settings.DOCUMENT_UPLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File is larger than {settings.DOCUMENT_UPLOAD_MAX_BYTES} bytes",
        )
    if not payload:
        raise HTTPException(status_code=422, detail="File is empty")

    # Include org_id in the key to separate organizations within the bucket
    # and make ownership visible in its prefix.
    key = f"documents/{org_id or 'no-org'}/{knowledge_base_id}/{uuid.uuid4()}{suffix}"
    from src.services.conversation_lifecycle import begin_attachment_operation, settle_operation
    operation = await begin_attachment_operation(session, uuid.UUID(org_id), knowledge_base_id, 'document_upload', uuid.uuid4(), resource_key=key)
    await object_storage.put_bytes(key, payload)

    # Lost create/upload replies remain pending. Deleting the source here could
    # race an indexing worker which already accepted the document.
    doc = await rag_client.create_document(
        org_id, str(knowledge_base_id), title=Path(filename).stem,
        source_key=key, source_filename=filename,
        **({"operation_id":str(operation.operation_key)} if operation else {}),
    )

    session.add(
        FileMetadata(
            org_id=uuid.UUID(str(org_id)),
            owning_conversation_id=operation.conversation_id if operation else None,
            storage_key=key,
            filename=filename,
            content_type=file.content_type,
            size_bytes=len(payload),
            uploaded_by=uuid.UUID(str(uploaded_by)) if uploaded_by else None,
            document_id=uuid.UUID(str(doc["id"])) if doc.get("id") else None,
        )
    )
    if operation is not None:
        await settle_operation(session, operation)
    await session.commit()

    return doc
