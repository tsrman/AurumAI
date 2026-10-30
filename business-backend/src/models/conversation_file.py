"""Files of a conversation: spreadsheets, documents, data.

An agent attaches one to its answer (origin "generated"): the row records the
durable tool operation, and the bytes live in the conversation's ``files/``
folder. The user uploads one for Hermes Agent's workspace (origin
"uploaded"): no operation, its user message claims it by setting the turn,
and the bytes live in ``files/uploads/``. Pictures are conversation images.
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
# Only the user's uploads may be pictures; an agent's pictures are conversation images.
UPLOAD_IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")


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
            "content_type IN (" + ",".join(f"'{value}'" for value in FILE_CONTENT_TYPES) + ") OR "
            "(origin = 'uploaded' AND content_type IN ("
            + ",".join(f"'{value}'" for value in UPLOAD_IMAGE_TYPES)
            + "))",
            name="ck_conversation_file_content_type",
        ),
        CheckConstraint("size_bytes > 0", name="ck_conversation_file_size"),
        CheckConstraint("length(name) BETWEEN 1 AND 255", name="ck_conversation_file_name"),
        CheckConstraint("origin IN ('generated','uploaded')", name="ck_conversation_file_origin"),
        CheckConstraint(
            "(origin = 'generated' AND turn_id IS NOT NULL AND execution_id IS NOT NULL "
            "AND operation_id IS NOT NULL) OR (origin = 'uploaded' AND execution_id IS NULL "
            "AND operation_id IS NULL)",
            name="ck_conversation_file_origin_operation",
        ),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    operation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    origin: Mapped[str] = mapped_column(Text, nullable=False, server_default="generated")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
