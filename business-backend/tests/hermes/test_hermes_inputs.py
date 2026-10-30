"""The files a user gave a Hermes conversation, as tool-worker puts them into
the sandbox: listed with their workspace names, served only while a Hermes
turn of the conversation is active, never unsent or foreign ones."""

import hashlib
import uuid
from typing import Any

import pytest

from src.models.conversation_file import ConversationFile
from src.routers import hermes_internal

CSV = b"region,sales\nNorth,10\n"


class Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def get_bytes(self, key: str) -> bytes:
        return self.objects[key]


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> Storage:
    fake = Storage()
    monkeypatch.setattr(hermes_internal, "object_storage", fake)
    return fake


async def _upload(env: Any, storage: Storage, name: str, data: bytes, *, claimed: bool = True, conversation: Any = None) -> uuid.UUID:
    file_id = uuid.uuid4()
    conversation_id = conversation or env.conversation_id
    key = f"conversation-images/{env.org_id}/{conversation_id}/files/uploads/{file_id}.csv"
    async with env.sessions() as db:
        db.add(
            ConversationFile(
                id=file_id,
                org_id=env.org_id,
                conversation_id=conversation_id,
                turn_id=env.turn_id if claimed else None,
                origin="uploaded",
                name=name,
                storage_key=key,
                content_type="text/csv",
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
        await db.commit()
    storage.objects[key] = data
    return file_id


async def test_claimed_uploads_are_listed_and_served_for_the_active_turn(hermes_env, storage) -> None:
    await hermes_env.open_session()
    first = await _upload(hermes_env, storage, "sales.csv", CSV)
    unsent = await _upload(hermes_env, storage, "draft.csv", CSV, claimed=False)

    listed = await hermes_env.internal("GET", "/inputs")
    content = await hermes_env.internal("GET", f"/inputs/{first}/content")
    hidden = await hermes_env.internal("GET", f"/inputs/{unsent}/content")
    unknown = await hermes_env.internal("GET", f"/inputs/{uuid.uuid4()}/content")

    assert listed.status_code == 200
    assert listed.json() == {
        "inputs": [
            {"fileId": str(first), "name": "sales.csv", "sizeBytes": len(CSV), "sha256": hashlib.sha256(CSV).hexdigest()}
        ]
    }
    assert content.status_code == 200 and content.content == CSV
    assert hidden.status_code == unknown.status_code == 404


async def test_nothing_is_served_once_the_turn_is_over(hermes_env, storage) -> None:
    await hermes_env.open_session()
    file_id = await _upload(hermes_env, storage, "sales.csv", CSV)
    await hermes_env.finish_turn()

    listed = await hermes_env.internal("GET", "/inputs")
    content = await hermes_env.internal("GET", f"/inputs/{file_id}/content")

    assert listed.status_code == content.status_code == 403
