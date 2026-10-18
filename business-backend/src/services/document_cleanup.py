from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.file_metadata import FileMetadata
from src.services.object_storage import ObjectStorage, object_storage
from src.services.rag_client import RagClient, rag_client


class DocumentCleanupService:
    """Coordinates retryable deletion across owned storage and the RAG service."""

    def __init__(self, *, storage: ObjectStorage, rag: RagClient) -> None:
        self._storage = storage
        self._rag = rag

    async def delete(
        self,
        session: AsyncSession,
        *,
        org_id: UUID,
        knowledge_base_id: UUID,
        document_id: str,
    ) -> None:
        try:
            canonical_document_id = UUID(document_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid document id",
            ) from exc

        from src.services.conversation_lifecycle import begin_attachment_operation, settle_operation
        operation = await begin_attachment_operation(session, org_id, knowledge_base_id, 'document_delete', uuid4())
        metadata = (
            await session.execute(
                select(FileMetadata)
                .where(
                    FileMetadata.org_id == org_id,
                    FileMetadata.document_id == canonical_document_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

        if metadata is not None:
            # The public caller has WRITE on this KB, not every KB in the org.
            # Uploads assign this immutable key server-side. Prove ownership
            # before touching storage, including retries where RAG returns 404.
            # Legacy/unscoped keys require operator recovery, never inference.
            prefix = f"documents/{org_id}/{knowledge_base_id}/"
            if not metadata.storage_key.startswith(prefix):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Document not found",
                )
            await self._storage.delete(metadata.storage_key)

        try:
            await self._rag.delete_document(
                str(org_id),
                str(knowledge_base_id),
                document_id,
            )
        except HTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND or metadata is None:
                raise

        if metadata is not None:
            await session.delete(metadata)
        if operation is not None:
            await settle_operation(session, operation)
        await session.commit()


document_cleanup_service = DocumentCleanupService(
    storage=object_storage,
    rag=rag_client,
)
