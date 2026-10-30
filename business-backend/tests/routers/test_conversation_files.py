import uuid
from collections.abc import AsyncIterator

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
from src.routers import conversation_files

CSV = b"month,revenue\nJan,10\n"


class _Storage:
    def __init__(self) -> None:
        self.reads: list[str] = []

    async def get_bytes(self, key: str) -> bytes:
        self.reads.append(key)
        return CSV


def _app(db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> FastAPI:
    app = FastAPI()
    app.include_router(conversation_files.router, prefix="/v1")

    async def session_override() -> AsyncIterator[AsyncSession]:
        yield db

    app.dependency_overrides[get_org_db_session] = session_override
    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        user_id=str(user_id), org_id=str(org_id), role="member"
    )
    return app


async def _conversation_with_file(
    db: AsyncSession, org: Organization, user: User, *, status: str = "active", name: str = "r\u00e9sum\u00e9 Q1.csv"
) -> tuple[uuid.UUID, uuid.UUID]:
    conversation = Conversation(org_id=org.id, user_id=user.id, model="m", status=status)
    db.add(conversation)
    await db.flush()
    turn, execution, operation = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    args = dict(o=org.id, c=conversation.id, u=user.id, t=turn, e=execution, op=operation)
    await db.execute(
        text(
            "INSERT INTO interaction_turns(id,org_id,conversation_id,owner_id,status,limits) VALUES(:t,:o,:c,:u,'active','{}')"
        ),
        args,
    )
    await db.execute(
        text(
            "INSERT INTO interaction_executions(id,org_id,turn_id,workflow_id,run_id,task_queue,phase) VALUES(:e,:o,:t,:wf,:e,'test','ordinary')"
        ),
        dict(args, wf=f"files-test-{execution}"),
    )
    await db.execute(
        text(
            "INSERT INTO tool_operations(id,org_id,turn_id,execution_id,invocation_id,kind,arguments_hash,status,result) VALUES(:op,:o,:t,:e,:op,'file_attach',:hash,'completed','{}')"
        ),
        dict(args, hash="a" * 64),
    )
    file_id = uuid.uuid4()
    db.add(
        ConversationFile(
            id=file_id,
            org_id=org.id,
            conversation_id=conversation.id,
            turn_id=turn,
            execution_id=execution,
            operation_id=operation,
            name=name,
            storage_key=f"conversation-images/{org.id}/{conversation.id}/files/{operation}.csv",
            content_type="text/csv",
            size_bytes=len(CSV),
            sha256="b" * 64,
        )
    )
    await db.commit()
    return conversation.id, file_id


@pytest.fixture
def storage(monkeypatch) -> _Storage:  # noqa: ANN001
    fake = _Storage()
    monkeypatch.setattr(conversation_files, "object_storage", fake)
    return fake


async def test_owner_downloads_the_file_as_an_attachment(db, test_org, test_user, storage) -> None:  # noqa: ANN001
    conversation_id, file_id = await _conversation_with_file(db, test_org, test_user)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org.id, test_user.id)), base_url="http://test"
    ) as client:
        response = await client.get(f"/v1/conversations/{conversation_id}/files/{file_id}/content")
    assert response.status_code == 200
    assert response.content == CSV
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"] == (
        "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9%20Q1.csv"
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["cache-control"].startswith("private")


async def test_strangers_deleted_conversations_and_unknown_files_get_404(db, test_org, test_user, storage) -> None:  # noqa: ANN001
    conversation_id, file_id = await _conversation_with_file(db, test_org, test_user)
    stranger = User(keycloak_id=uuid.uuid4(), email=f"s-{uuid.uuid4().hex[:8]}@example.com", org_id=test_org.id)
    db.add(stranger)
    await db.commit()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org.id, stranger.id)), base_url="http://test"
    ) as client:
        assert (await client.get(f"/v1/conversations/{conversation_id}/files/{file_id}/content")).status_code == 404

    deleted_id, deleted_file = await _conversation_with_file(db, test_org, test_user, status="deleted")
    _, other_file = await _conversation_with_file(db, test_org, test_user)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(db, test_org.id, test_user.id)), base_url="http://test"
    ) as client:
        for url in (
            f"/v1/conversations/{deleted_id}/files/{deleted_file}/content",
            f"/v1/conversations/{conversation_id}/files/{uuid.uuid4()}/content",
            # A file of another conversation is not served by its id.
            f"/v1/conversations/{conversation_id}/files/{other_file}/content",
        ):
            assert (await client.get(url)).status_code == 404
    assert storage.reads == []


async def test_files_cascade_with_their_conversation_identity(db, test_org, test_user, storage) -> None:  # noqa: ANN001
    conversation_id, file_id = await _conversation_with_file(db, test_org, test_user)
    await db.execute(text("DELETE FROM conversation_identities WHERE id = :id"), {"id": conversation_id})
    await db.commit()
    assert await db.get(ConversationFile, file_id) is None
