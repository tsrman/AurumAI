import uuid
from typing import Optional

from sqlalchemy import BigInteger, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import OrgScopedModel


class FileMetadata(OrgScopedModel):
    """Describe a stored file and its uploader.

    MinIO stores source bytes. Metadata supports upload lists, storage totals
    and deleting the object together with its associated document.
    """

    __tablename__ = "file_metadata"

    owning_conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # Unique object key: one storage object maps to one metadata row.
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[Optional[str]] = mapped_column(String(127), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Uploader is not a foreign key: the user can be deleted while upload
    # history remains, as with audit_logs.
    uploaded_by: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    # The rag-api document reference is not a cross-database foreign key;
    # documents live in the separate rag_platform database.
    document_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
