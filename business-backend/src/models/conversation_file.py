"""Files an agent attached to its answer: spreadsheets, documents, data.

Bytes live in object storage under ``storage_key``, in the conversation's
``files/`` folder; the row records the durable tool operation that attached
it. Pictures become conversation images instead.
"""

import uuid

from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import OrgScopedModel

# Register the referenced interaction tables for isolated metadata imports.
from src.models import interaction  # noqa: F401

FILE_CONTENT_TYPES = (
    "text/csv",
    "text/plain",
    "text/markdown",
    "application/json",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/zip",
)


class ConversationFile(OrgScopedModel):
    __tablename__ = "conversation_files"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_conversation_file_org_id"),
        UniqueConstraint("storage_key", name="uq_conversation_file_storage_key"),
        UniqueConstraint("org_id", "operation_id", name="uq_conversation_file_operation"),
        ForeignKeyConstraint(
            ["org_id", "conversation_id"],
            ["conversation_identities.org_id", "conversation_identities.id"],
            name="fk_conversation_file_conversation",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "turn_id", "execution_id", "operation_id"],
            [
                "tool_operations.org_id",
                "tool_operations.turn_id",
                "tool_operations.execution_id",
                "tool_operations.id",
            ],
            name="fk_conversation_file_operation",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "content_type IN (" + ",".join(f"'{value}'" for value in FILE_CONTENT_TYPES) + ")",
            name="ck_conversation_file_content_type",
        ),
        CheckConstraint("size_bytes > 0", name="ck_conversation_file_size"),
        CheckConstraint("length(name) BETWEEN 1 AND 255", name="ck_conversation_file_name"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
