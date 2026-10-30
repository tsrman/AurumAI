import uuid
from typing import Any

import httpx
import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src import hermes_activities
from src.hermes_file_workflow import HERMES_FILE_WORKFLOW, HermesFileAttach, HermesFileAttachWorkflow

QUEUE = "tools-test"
CALL = {"session_id": "a" * 32, "operation_id": str(uuid.uuid4()), "path": "out/data.csv", "session_token": "t"}


async def _run(fake: Any) -> Any:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=QUEUE, workflows=[HermesFileAttachWorkflow], activities=[fake]):
            # The backend starts it by name with a plain dict, as here.
            return await env.client.execute_workflow(
                HERMES_FILE_WORKFLOW, CALL, id=f"hermes-file-{CALL['operation_id']}", task_queue=QUEUE
            )


@pytest.mark.asyncio
async def test_the_workflow_keeps_only_what_the_activity_returns() -> None:
    seen: list[HermesFileAttach] = []

    @activity.defn(name="hermes_attach_file")
    async def fake(call: HermesFileAttach) -> dict[str, Any]:
        seen.append(call)
        return {"file": {"fileId": "f"}}

    assert await _run(fake) == {"file": {"fileId": "f"}}
    assert seen == [HermesFileAttach(**CALL)]


@pytest.mark.asyncio
async def test_a_failing_activity_ends_as_a_bare_code_after_bounded_retries() -> None:
    attempts: list[int] = []

    @activity.defn(name="hermes_attach_file")
    async def fake(call: HermesFileAttach) -> dict[str, Any]:
        attempts.append(1)
        raise ApplicationError("http://skill-executor:8080 refused token=abc")

    assert await _run(fake) == {"error": "file_unavailable"}
    assert len(attempts) == 3


def _transport(handler: Any) -> Any:
    real = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(handler), **kwargs)

    return client


@pytest.mark.asyncio
async def test_the_activity_hands_the_sandbox_bytes_to_the_backend(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=b"a,b\n1,2\n")
        return httpx.Response(200, json={"fileId": "f", "name": "data.csv"})

    monkeypatch.setattr(hermes_activities.httpx, "AsyncClient", _transport(handler))

    result = await hermes_activities.hermes_attach_file(HermesFileAttach(**CALL))

    assert result == {"file": {"fileId": "f", "name": "data.csv"}}
    read, stored = requests
    assert read.url.path == f"/hermes/sandboxes/{'a' * 32}/files" and read.url.params["path"] == "out/data.csv"
    assert stored.url.path == f"/v1/internal/hermes/files/{CALL['operation_id']}"
    assert stored.headers["authorization"] == "Bearer t" and stored.content == b"a,b\n1,2\n"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read", "store", "expected"),
    [
        (httpx.Response(403, json={"detail": {"code": "file_not_allowed"}}), None, {"error": "file_not_allowed"}),
        # business-backend's error body.
        (httpx.Response(200, content=b"x"),
         httpx.Response(422, json={"error": {"code": "FORBIDDEN", "message": {"code": "file_type_unsupported"}}}),
         {"error": "file_type_unsupported"}),
    ],
)
async def test_refusals_come_back_as_their_code(monkeypatch, read, store, expected) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return read if request.method == "GET" else store

    monkeypatch.setattr(hermes_activities.httpx, "AsyncClient", _transport(handler))
    assert await hermes_activities.hermes_attach_file(HermesFileAttach(**CALL)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 500])
async def test_other_failures_are_retried_and_carry_no_detail(monkeypatch, status) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": {"code": "some_internal_url_http://x"}})

    monkeypatch.setattr(hermes_activities.httpx, "AsyncClient", _transport(handler))
    with pytest.raises(ApplicationError) as failed:
        await hermes_activities.hermes_attach_file(HermesFileAttach(**CALL))
    assert failed.value.message == "hermes_file_unavailable" and failed.value.details == ()
