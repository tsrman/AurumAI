"""Files Hermes attaches over the MCP bridge: an operation per call, bytes
through the upload route, a picture as a conversation image, and nothing
attached once the call gave up."""

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from src.core.config import settings
from src.models.conversation_file import ConversationFile
from src.models.conversation_image import ConversationImage
from src.models.interaction import ToolOperation
from src.services import hermes_files
from tests.hermes.test_hermes_mcp import _call, _rpc, _tool_payload

CSV = b"month,revenue\nJan,10\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (64).to_bytes(4, "big") + (32).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"


class Storage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_bytes(self, key: str, data: bytes, **_: Any) -> None:
        self.objects[key] = data


class Handle:
    def __init__(self, result: Any, delay: float = 0) -> None:
        self._result, self._delay = result, delay

    async def result(self) -> Any:
        await asyncio.sleep(self._delay)
        return self._result


class ToolWorker:
    """Starts the file workflow as tool-worker would: the sandbox's bytes go to
    the upload route, and the route's answer becomes the workflow's result."""

    def __init__(
        self, env: Any, data: bytes, *, delay: float = 0, upload: bool = True, lost: bool = False
    ) -> None:
        # lost: the upload stores the file, but its answer never comes back.
        self.env, self.data, self.delay, self.upload, self.lost = env, data, delay, upload, lost
        self.starts: list[dict[str, Any]] = []
        self.uploads: list[Any] = []

    async def deliver(self, call: dict[str, Any]) -> Any:
        response = await self.env.internal(
            "POST",
            f"/files/{call['operation_id']}",
            params={"path": call["path"]},
            content=self.data,
        )
        self.uploads.append(response)
        if response.status_code == 200:
            return {"file": response.json()}
        return {"error": _code(response)}

    async def start_workflow(self, name: str, call: dict[str, Any], **options: Any) -> Handle:
        self.starts.append({"name": name, "call": call, **options})
        if self.lost:
            await self.deliver(call)
            return Handle({"error": "file_unavailable"}, self.delay)
        result = await self.deliver(call) if self.upload and not self.delay else {"error": "file_unavailable"}
        return Handle(result, self.delay)

    def get_workflow_handle(self, workflow_id: str) -> Handle:
        raise AssertionError("not expected")


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> Storage:
    fake = Storage()
    monkeypatch.setattr(hermes_files, "object_storage", fake)
    return fake


def _code(response: httpx.Response) -> str:
    return response.json()["error"]["message"]["code"]


def _worker(monkeypatch: pytest.MonkeyPatch, worker: ToolWorker) -> ToolWorker:
    monkeypatch.setattr(hermes_files, "get_temporal_client", lambda: worker)
    return worker


async def _rows(env: Any, model: Any) -> list[Any]:
    async with env.sessions() as db:
        return list((await db.scalars(select(model).where(model.org_id == env.org_id))).all())


async def test_a_csv_is_stored_as_a_file_of_the_turn(hermes_env, storage, monkeypatch) -> None:
    worker = _worker(monkeypatch, ToolWorker(hermes_env, CSV))
    opened = await hermes_env.open_session()

    is_error, payload = _tool_payload(
        await _rpc(hermes_env, opened["token"], _call("attach_file", {"path": "/workspace/out/report.csv"}))
    )

    assert not is_error
    [row] = await _rows(hermes_env, ConversationFile)
    assert payload == {"fileId": str(row.id), "name": "report.csv", "contentType": "text/csv", "sizeBytes": len(CSV)}
    assert (row.turn_id, row.execution_id, row.conversation_id) == (
        hermes_env.turn_id,
        hermes_env.execution_id,
        hermes_env.conversation_id,
    )
    assert storage.objects == {row.storage_key: CSV}
    assert row.storage_key == f"conversation-images/{hermes_env.org_id}/{hermes_env.conversation_id}/files/{row.operation_id}.csv"
    [start] = worker.starts
    assert start["name"] == "HermesFileAttachWorkflow"
    assert start["id"] == f"hermes-file-{row.operation_id}"
    assert start["task_queue"] == settings.HERMES_SKILL_TASK_QUEUE
    assert start["call"]["session_id"] == uuid.UUID(opened["sessionId"]).hex
    assert start["call"]["path"] == "/workspace/out/report.csv"
    [operation] = await _rows(hermes_env, ToolOperation)
    assert (operation.kind, operation.status, operation.result) == ("file_attach", "completed", payload)


async def test_a_picture_becomes_a_generated_image_of_the_turn(hermes_env, storage, monkeypatch) -> None:
    _worker(monkeypatch, ToolWorker(hermes_env, PNG))
    opened = await hermes_env.open_session()

    is_error, payload = _tool_payload(
        await _rpc(hermes_env, opened["token"], _call("attach_file", {"path": "chart.png"}))
    )

    assert not is_error
    [image] = await _rows(hermes_env, ConversationImage)
    assert payload == {"imageId": str(image.id), "contentType": "image/png", "width": 64, "height": 32}
    assert (image.origin, image.turn_id, image.operation_id is not None) == ("generated", hermes_env.turn_id, True)
    assert image.storage_key.endswith(f"/{image.operation_id}.png")
    assert await _rows(hermes_env, ConversationFile) == []


async def test_a_repeated_request_returns_the_stored_file(hermes_env, storage, monkeypatch) -> None:
    worker = _worker(monkeypatch, ToolWorker(hermes_env, CSV))
    opened = await hermes_env.open_session()
    call = _call("attach_file", {"path": "data.csv"})

    first = _tool_payload(await _rpc(hermes_env, opened["token"], call))
    again = _tool_payload(await _rpc(hermes_env, opened["token"], call))

    assert first == again and len(worker.starts) == 1
    assert len(await _rows(hermes_env, ConversationFile)) == 1


async def test_bytes_that_are_no_known_file_fail_the_operation(hermes_env, storage, monkeypatch) -> None:
    _worker(monkeypatch, ToolWorker(hermes_env, b"MZ\x90\x00binary"))
    opened = await hermes_env.open_session()

    is_error, payload = _tool_payload(
        await _rpc(hermes_env, opened["token"], _call("attach_file", {"path": "tool.exe"}))
    )

    assert is_error and payload == {"error": "file_type_unsupported"}
    [operation] = await _rows(hermes_env, ToolOperation)
    assert (operation.status, operation.error_code) == ("failed", "file_type_unsupported")
    assert storage.objects == {}


async def test_a_call_that_gave_up_refuses_bytes_arriving_later(hermes_env, storage, monkeypatch) -> None:
    monkeypatch.setattr(settings, "HERMES_FILE_TIMEOUT_SECONDS", 0.05)
    worker = _worker(monkeypatch, ToolWorker(hermes_env, CSV, delay=1))
    opened = await hermes_env.open_session()

    is_error, payload = _tool_payload(
        await _rpc(hermes_env, opened["token"], _call("attach_file", {"path": "late.csv"}))
    )

    assert is_error and payload == {"error": "file_timeout"}
    late = await worker.deliver(worker.starts[0]["call"])
    assert late == {"error": "hermes_turn_inactive"}
    assert await _rows(hermes_env, ConversationFile) == [] and storage.objects == {}


async def test_a_turn_attaches_at_most_ten_files(hermes_env, storage, monkeypatch) -> None:
    _worker(monkeypatch, ToolWorker(hermes_env, CSV))
    opened = await hermes_env.open_session()

    for index in range(10):
        is_error, _ = _tool_payload(
            await _rpc(hermes_env, opened["token"], _call("attach_file", {"path": f"f{index}.csv"}, index))
        )
        assert not is_error
    is_error, payload = _tool_payload(
        await _rpc(hermes_env, opened["token"], _call("attach_file", {"path": "f10.csv"}, 10))
    )

    assert is_error and payload == {"error": "file_limit"}
    assert len(await _rows(hermes_env, ConversationFile)) == 10


async def test_the_upload_route_takes_only_an_open_operation_of_its_conversation(hermes_env, storage, monkeypatch) -> None:
    _worker(monkeypatch, ToolWorker(hermes_env, CSV, upload=False))
    opened = await hermes_env.open_session()
    _tool_payload(await _rpc(hermes_env, opened["token"], _call("attach_file", {"path": "x.csv"})))
    [operation] = await _rows(hermes_env, ToolOperation)

    unknown = await hermes_env.internal("POST", f"/files/{uuid.uuid4()}", params={"path": "x.csv"}, content=CSV)
    # The workflow reported a failure, so the operation is closed.
    closed = await hermes_env.internal("POST", f"/files/{operation.id}", params={"path": "x.csv"}, content=CSV)
    # A Hermes token never reaches the internal routes.
    async with httpx.AsyncClient(transport=ASGITransport(app=hermes_env.app), base_url="http://test") as client:
        hermes_token = await client.post(
            f"/v1/internal/hermes/files/{operation.id}",
            params={"path": "x.csv"},
            content=CSV,
            headers={"Authorization": f"Bearer {opened['token']}"},
        )

    assert unknown.status_code == 404 and _code(unknown) == "file_operation_not_found"
    assert closed.status_code == 409 and _code(closed) == "hermes_turn_inactive"
    assert hermes_token.status_code in (401, 403)
    assert await _rows(hermes_env, ConversationFile) == [] and storage.objects == {}


async def test_the_answer_of_the_turn_lists_its_files_and_images_for_its_owner_only(
    hermes_env, storage, monkeypatch
) -> None:
    from src.repositories.conversation_file_repository import turn_files
    from src.repositories.image_repository import ImageRepository

    opened = await hermes_env.open_session()
    _worker(monkeypatch, ToolWorker(hermes_env, CSV))
    _, csv = _tool_payload(await _rpc(hermes_env, opened["token"], _call("attach_file", {"path": "a.csv"}, 1)))
    _worker(monkeypatch, ToolWorker(hermes_env, PNG))
    _, png = _tool_payload(await _rpc(hermes_env, opened["token"], _call("attach_file", {"path": "b.png"}, 2)))
    env = hermes_env
    images = ImageRepository(env.org_id)

    async with env.sessions() as db:
        assert await turn_files(db, env.org_id, env.conversation_id, env.user_id, env.turn_id) == [csv]
        assert await images.turn_image_ids(db, env.conversation_id, env.user_id, env.turn_id) == [png["imageId"]]
        stranger = uuid.uuid4()
        assert await turn_files(db, env.org_id, env.conversation_id, stranger, env.turn_id) == []
        assert await images.turn_image_ids(db, env.conversation_id, stranger, env.turn_id) == []


@pytest.mark.parametrize("delay", [0, 1], ids=["answer-lost", "timed-out"])
async def test_a_stored_file_reaches_the_model_although_the_workflow_said_otherwise(
    hermes_env, storage, monkeypatch, delay
) -> None:
    monkeypatch.setattr(settings, "HERMES_FILE_TIMEOUT_SECONDS", 0.05)
    _worker(monkeypatch, ToolWorker(hermes_env, CSV, delay=delay, lost=True))
    opened = await hermes_env.open_session()

    is_error, payload = _tool_payload(
        await _rpc(hermes_env, opened["token"], _call("attach_file", {"path": "/workspace/kept.csv"}))
    )

    [row] = await _rows(hermes_env, ConversationFile)
    assert not is_error and payload["fileId"] == str(row.id)
    [operation] = await _rows(hermes_env, ToolOperation)
    assert operation.status == "completed"


async def test_the_users_own_upload_to_the_turn_is_not_shown_under_the_answer(hermes_env, storage, monkeypatch) -> None:
    from src.repositories.image_repository import ImageRepository

    opened = await hermes_env.open_session()
    _worker(monkeypatch, ToolWorker(hermes_env, PNG))
    _, generated = _tool_payload(await _rpc(hermes_env, opened["token"], _call("attach_file", {"path": "c.png"})))
    env = hermes_env
    async with env.sessions() as db:
        upload = uuid.uuid4()
        db.add(
            ConversationImage(
                id=upload,
                org_id=env.org_id,
                conversation_id=env.conversation_id,
                turn_id=env.turn_id,
                origin="uploaded",
                storage_key=f"conversation-images/{env.org_id}/{env.conversation_id}/uploads/{upload}.png",
                content_type="image/png",
                size_bytes=len(PNG),
            )
        )
        await db.commit()
        shown = await ImageRepository(env.org_id).turn_image_ids(db, env.conversation_id, env.user_id, env.turn_id)

    assert shown == [generated["imageId"]]
