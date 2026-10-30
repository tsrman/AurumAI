"""Files the user uploads for Hermes Agent's workspace: storage, limits,
claiming, workspace names and the routes."""

import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo, get_current_user
from src.core.dependencies import get_org_db_session
from src.models.conversation import Conversation
from src.models.conversation_file import ConversationFile
from src.models.organization import Organization
from src.models.user import User
from src.routers import chat
from src.services import file_uploads
from src.services.conversation_files import FileRejected
from src.services.file_uploads import (
    FileUploadError,
    check_unclaimed_files,
    claim_files,
    delete_unclaimed_file,
    store_uploaded_file,
    workspace_inputs,
    workspace_names,
)

CSV = b"region,sales\nNorth,10\n"


class Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def put_bytes(self, key: str, data: bytes) -> None:
        self.objects[key] = data

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


async def _conversation(db: AsyncSession, org: Organization, user: User) -> Conversation:
    conversation = Conversation(org_id=org.id, user_id=user.id, model="m")
    db.add(conversation)
    await db.commit()
    return conversation


async def _turn(db: AsyncSession, org: Organization, user: User, conversation: Conversation) -> uuid.UUID:
    turn = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO interaction_turns(id,org_id,conversation_id,owner_id,status,limits) VALUES(:t,:o,:c,:u,'active','{}')"
        ),
        dict(t=turn, o=org.id, c=conversation.id, u=user.id),
    )
    await db.commit()
    return turn


async def test_an_upload_is_stored_by_its_bytes_with_no_turn(db, test_org, test_user) -> None:  # noqa: ANN001
    conversation = await _conversation(db, test_org, test_user)
    storage = Storage()

    uploaded = await store_uploaded_file(db, storage, test_org.id, conversation.id, "../q1 sales.CSV", CSV)

    assert (uploaded.origin, uploaded.turn_id, uploaded.operation_id) == ("uploaded", None, None)
    assert (uploaded.name, uploaded.content_type, uploaded.size_bytes) == ("q1 sales.csv", "text/csv", len(CSV))
    assert uploaded.storage_key == (
        f"conversation-images/{test_org.id}/{conversation.id}/files/uploads/{uploaded.id}.csv"
    )
    assert storage.objects == {uploaded.storage_key: CSV}
    with pytest.raises(FileRejected):
        await store_uploaded_file(db, storage, test_org.id, conversation.id, "tool.exe", b"MZ\x90\x00")


async def test_a_conversation_holds_a_bounded_number_and_size_of_uploads(db, test_org, test_user, monkeypatch) -> None:  # noqa: ANN001
    conversation = await _conversation(db, test_org, test_user)
    monkeypatch.setattr(file_uploads, "MAX_UPLOADS_PER_CONVERSATION", 2)
    monkeypatch.setattr(file_uploads, "MAX_UPLOAD_BYTES_PER_CONVERSATION", len(CSV) * 3)
    storage = Storage()
    for _ in range(2):
        await store_uploaded_file(db, storage, test_org.id, conversation.id, "a.csv", CSV)

    with pytest.raises(FileUploadError) as count:
        await store_uploaded_file(db, storage, test_org.id, conversation.id, "a.csv", CSV)
    monkeypatch.setattr(file_uploads, "MAX_UPLOADS_PER_CONVERSATION", 5)
    with pytest.raises(FileUploadError) as size:
        await store_uploaded_file(db, storage, test_org.id, conversation.id, "a.csv", CSV * 2)

    assert count.value.code == size.value.code == "file_limit"


async def test_uploads_are_claimed_once_and_get_stable_workspace_names(db, test_org, test_user) -> None:  # noqa: ANN001
    conversation = await _conversation(db, test_org, test_user)
    storage = Storage()
    uploads = []
    for minute, (name, data) in enumerate(
        (("data.csv", CSV), ("data.csv", CSV), ("notes.md", b"# Notes\n"), ("data.csv", CSV))
    ):
        uploaded = await store_uploaded_file(db, storage, test_org.id, conversation.id, name, data)
        # One request each in the composer: their times order the names.
        await db.execute(
            text("UPDATE conversation_files SET created_at = now() + make_interval(mins => :m) WHERE id = :id"),
            dict(m=minute, id=uploaded.id),
        )
        uploads.append(uploaded)
    await db.commit()
    first, second, other, unsent = uploads
    turn = await _turn(db, test_org, test_user, conversation)

    await check_unclaimed_files(db, test_org.id, conversation.id, [first.id, second.id, other.id])
    claimed = await claim_files(db, test_org.id, conversation.id, turn, [second.id, first.id, other.id])
    await db.commit()

    names = await workspace_names(db, test_org.id, conversation.id)
    assert names == {first.id: "data.csv", second.id: "data (2).csv", other.id: "notes.md"}
    assert [item["path"] for item in claimed] == [
        "/workspace/inputs/data (2).csv",
        "/workspace/inputs/data.csv",
        "/workspace/inputs/notes.md",
    ]
    inputs = await workspace_inputs(db, test_org.id, conversation.id)
    assert [(item["name"], item["sizeBytes"]) for item in inputs] == [
        ("data (2).csv", len(CSV)),
        ("data.csv", len(CSV)),
        ("notes.md", 8),
    ]
    assert unsent.id not in names
    for again in ([first.id], [uuid.uuid4()]):
        with pytest.raises(FileUploadError):
            await check_unclaimed_files(db, test_org.id, conversation.id, again)
        with pytest.raises(FileUploadError):
            await claim_files(db, test_org.id, conversation.id, turn, again)
    with pytest.raises(FileUploadError) as many:
        await check_unclaimed_files(db, test_org.id, conversation.id, [uuid.uuid4() for _ in range(11)])
    assert many.value.code == "file_limit"


async def test_only_an_unsent_upload_can_be_removed(db, test_org, test_user) -> None:  # noqa: ANN001
    conversation = await _conversation(db, test_org, test_user)
    storage = Storage()
    sent = await store_uploaded_file(db, storage, test_org.id, conversation.id, "a.csv", CSV)
    unsent = await store_uploaded_file(db, storage, test_org.id, conversation.id, "a.csv", CSV)
    await claim_files(db, test_org.id, conversation.id, await _turn(db, test_org, test_user, conversation), [sent.id])

    assert await delete_unclaimed_file(db, test_org.id, conversation.id, sent.id) is None
    assert await delete_unclaimed_file(db, test_org.id, conversation.id, unsent.id) == unsent.storage_key


def _app(db: AsyncSession, user: UserInfo) -> FastAPI:
    app = FastAPI()
    app.include_router(chat.router, prefix="/v1")

    async def session_override() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_org_db_session] = session_override
    app.dependency_overrides[get_current_user] = lambda: user
    return app


async def test_the_routes_store_and_remove_uploads_only_with_hermes(db, test_org, test_user, monkeypatch) -> None:  # noqa: ANN001
    conversation = await _conversation(db, test_org, test_user)
    storage = Storage()
    monkeypatch.setattr(chat, "object_storage", storage)
    capability = AsyncMock(return_value=True)
    monkeypatch.setattr("src.core.authorization_dependencies.authorization_service.has_capability", capability)
    monkeypatch.setattr("src.services.hermes_access.authorization_service.has_capability", capability)
    owner = UserInfo(user_id=str(test_user.id), org_id=str(test_org.id), role="member")
    url = f"/v1/conversations/{conversation.id}/files"
    files = {"file": ("sales.csv", CSV, "text/csv")}

    async with httpx.AsyncClient(transport=ASGITransport(app=_app(db, owner)), base_url="http://test") as client:
        monkeypatch.setattr(chat.settings, "HERMES_ENABLED", False)
        disabled = await client.post(url, files=files)
        monkeypatch.setattr(chat.settings, "HERMES_ENABLED", True)
        uploaded = await client.post(url, files=files)
        rejected = await client.post(url, files={"file": ("x.exe", b"MZ\x90\x00", "application/octet-stream")})
        file_id = uploaded.json()["data"]["fileId"]
        removed = await client.delete(f"{url}/{file_id}")
        missing = await client.delete(f"{url}/{file_id}")
        capability.return_value = False
        denied = await client.post(url, files=files)

    assert disabled.status_code == 403 and denied.status_code == 403
    assert uploaded.status_code == 201
    assert uploaded.json()["data"] == {
        "fileId": file_id,
        "name": "sales.csv",
        "contentType": "text/csv",
        "sizeBytes": len(CSV),
    }
    assert rejected.status_code == 422 and rejected.json()["detail"] == {"code": "file_type_unsupported"}
    assert removed.status_code == 204 and missing.status_code == 404
    assert await db.get(ConversationFile, uuid.UUID(file_id)) is None
    assert len(storage.deleted) == 1
