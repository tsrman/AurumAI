"""Files the user uploads for Hermes Agent's workspace.

As image uploads: a file is stored and recorded with no turn, and the user
message that carries it claims it by setting the turn; until then the user
can remove it. Its type comes from its bytes, as for files an agent attaches.
Claimed uploads are what the sandbox receives, each under one workspace name.
"""

import hashlib
import re
from collections.abc import Sequence
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.conversation_file import ConversationFile
from src.services.conversation_files import MAX_FILE_BYTES
from src.services.conversation_files import FileKind, FileRejected, display_name, sniff
from src.services.image_content import ImageContentError, inspect_image

MAX_FILES_PER_MESSAGE = 10
# Bounded so that a new sandbox receives them within one hydration and its
# snapshot stays well inside skill-executor's limit.
MAX_UPLOADS_PER_CONVERSATION = 20
MAX_UPLOAD_BYTES_PER_CONVERSATION = 200 * 1024 * 1024
# Where the sandbox holds them, relative to its workspace.
INPUTS_DIR = "inputs"


class FileUploadError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class UploadStorage(Protocol):
    async def put_bytes(self, key: str, data: bytes) -> None: ...


def upload_kind(data: bytes, name: str) -> FileKind:
    """What an upload is, from its bytes: a picture Hermes works on in its
    terminal, or one of the conversation file types."""
    if data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        try:
            info = inspect_image(data, max_bytes=MAX_FILE_BYTES)
        except ImageContentError:
            raise FileRejected("file_type_unsupported") from None
        return FileKind(info.content_type, info.extension)
    return sniff(data, name)


def uploaded_file_key(org_id: UUID, conversation_id: UUID, file_id: UUID, kind: FileKind) -> str:
    return f"conversation-images/{org_id}/{conversation_id}/files/uploads/{file_id}.{kind.extension}"


async def store_uploaded_file(
    session: AsyncSession,
    storage: UploadStorage,
    org_id: UUID,
    conversation_id: UUID,
    name: str,
    data: bytes,
) -> ConversationFile:
    """Check and store one upload; raises FileRejected or FileUploadError.
    The caller holds the conversation, so the limits are counted exactly."""
    kind = upload_kind(data, name)
    count, total = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(ConversationFile.size_bytes), 0)).where(
                ConversationFile.org_id == org_id,
                ConversationFile.conversation_id == conversation_id,
                ConversationFile.origin == "uploaded",
            )
        )
    ).one()
    if count >= MAX_UPLOADS_PER_CONVERSATION or total + len(data) > MAX_UPLOAD_BYTES_PER_CONVERSATION:
        raise FileUploadError("file_limit")
    file_id = uuid4()
    key = uploaded_file_key(org_id, conversation_id, file_id, kind)
    await storage.put_bytes(key, data)
    uploaded = ConversationFile(
        id=file_id,
        org_id=org_id,
        conversation_id=conversation_id,
        origin="uploaded",
        name=display_name(name, kind),
        storage_key=key,
        content_type=kind.content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    session.add(uploaded)
    await session.flush()
    return uploaded


async def delete_unclaimed_file(
    session: AsyncSession, org_id: UUID, conversation_id: UUID, file_id: UUID
) -> str | None:
    """Remove an upload no message carries yet; returns its storage key."""
    return await session.scalar(
        delete(ConversationFile)
        .where(
            ConversationFile.org_id == org_id,
            ConversationFile.conversation_id == conversation_id,
            ConversationFile.id == file_id,
            ConversationFile.origin == "uploaded",
            ConversationFile.turn_id.is_(None),
        )
        .returning(ConversationFile.storage_key)
    )


async def check_unclaimed_files(
    session: AsyncSession, org_id: UUID, conversation_id: UUID, file_ids: Sequence[UUID]
) -> None:
    """Every id must be an upload of this conversation no message has used yet."""
    wanted = tuple(dict.fromkeys(file_ids))
    if len(wanted) > MAX_FILES_PER_MESSAGE:
        raise FileUploadError("file_limit")
    if not wanted:
        return
    found = set(
        await session.scalars(
            select(ConversationFile.id).where(
                ConversationFile.org_id == org_id,
                ConversationFile.conversation_id == conversation_id,
                ConversationFile.origin == "uploaded",
                ConversationFile.turn_id.is_(None),
                ConversationFile.id.in_(wanted),
            )
        )
    )
    if found != set(wanted):
        raise FileUploadError("file_not_available")


async def claim_files(
    session: AsyncSession,
    org_id: UUID,
    conversation_id: UUID,
    turn_id: UUID,
    file_ids: Sequence[UUID],
) -> list[dict[str, Any]]:
    """Bind the uploads to the turn whose user message carries them; returns
    them as the message lists them, in the order the user gave."""
    wanted = tuple(dict.fromkeys(file_ids))
    if not wanted:
        return []
    rows = {
        row.id: row
        for row in await session.scalars(
            update(ConversationFile)
            .where(
                ConversationFile.org_id == org_id,
                ConversationFile.conversation_id == conversation_id,
                ConversationFile.origin == "uploaded",
                ConversationFile.turn_id.is_(None),
                ConversationFile.id.in_(wanted),
            )
            .values(turn_id=turn_id)
            .returning(ConversationFile)
        )
    }
    if set(rows) != set(wanted):
        raise FileUploadError("file_not_available")
    names = await workspace_names(session, org_id, conversation_id)
    return [
        {
            "fileId": str(file_id),
            "name": rows[file_id].name,
            "contentType": rows[file_id].content_type,
            "sizeBytes": rows[file_id].size_bytes,
            "path": f"/workspace/{INPUTS_DIR}/{names[file_id]}",
        }
        for file_id in wanted
    ]


def _numbered(name: str, number: int) -> str:
    stem, dot, extension = name.rpartition(".")
    return f"{stem} ({number}).{extension}" if dot else f"{name} ({number})"


async def workspace_names(session: AsyncSession, org_id: UUID, conversation_id: UUID) -> dict[UUID, str]:
    """Each claimed upload's file name in the sandbox's inputs folder: its own
    name, numbered when an earlier upload has it. Earlier uploads keep theirs,
    so a name, once given, never changes."""
    rows = await session.execute(
        select(ConversationFile.id, ConversationFile.name)
        .where(
            ConversationFile.org_id == org_id,
            ConversationFile.conversation_id == conversation_id,
            ConversationFile.origin == "uploaded",
            ConversationFile.turn_id.is_not(None),
        )
        .order_by(ConversationFile.created_at, ConversationFile.id)
    )
    taken: set[str] = set()
    names: dict[UUID, str] = {}
    for file_id, name in rows:
        candidate, number = name, 1
        while candidate.lower() in taken:
            number += 1
            candidate = _numbered(name, number)
        taken.add(candidate.lower())
        names[file_id] = candidate
    return names


_SAFE = re.compile(r"^[^/\x00]{1,255}$")


async def workspace_inputs(session: AsyncSession, org_id: UUID, conversation_id: UUID) -> list[dict[str, Any]]:
    """Every claimed upload as the sandbox receives it: name, size and SHA-256."""
    names = await workspace_names(session, org_id, conversation_id)
    if not names:
        return []
    rows = await session.scalars(
        select(ConversationFile).where(
            ConversationFile.org_id == org_id,
            ConversationFile.conversation_id == conversation_id,
            ConversationFile.id.in_(tuple(names)),
        )
    )
    inputs = [
        {
            "fileId": str(row.id),
            "name": names[row.id],
            "sizeBytes": row.size_bytes,
            "sha256": row.sha256,
        }
        for row in rows
        if _SAFE.fullmatch(names[row.id])
    ]
    return sorted(inputs, key=lambda item: item["name"])


async def claimed_upload_key(
    session: AsyncSession, org_id: UUID, conversation_id: UUID, file_id: UUID
) -> str | None:
    """The storage key of an upload of this conversation a message carries."""
    key: str | None = await session.scalar(
        select(ConversationFile.storage_key).where(
            ConversationFile.org_id == org_id,
            ConversationFile.conversation_id == conversation_id,
            ConversationFile.id == file_id,
            ConversationFile.origin == "uploaded",
            ConversationFile.turn_id.is_not(None),
        )
    )
    return key
