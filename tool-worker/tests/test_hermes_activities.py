import asyncio
import json
from typing import Any

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from src import hermes_activities

SESSION = "0123456789abcdef0123456789abcdef"
SESSION_UUID = "01234567-89ab-cdef-0123-456789abcdef"


class Router:
    """Answers httpx requests by method and path, and records them."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        answer = self.routes[(request.method, request.url.path)]
        return answer(request) if callable(answer) else answer


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    def install(routes: dict[tuple[str, str], Any]) -> Router:
        router = Router(routes)
        real = httpx.AsyncClient

        def client(**kwargs: Any) -> httpx.AsyncClient:
            return real(transport=httpx.MockTransport(router), **kwargs)

        monkeypatch.setattr(hermes_activities.httpx, "AsyncClient", client)
        return router

    return install


def _created(status: int = 201) -> httpx.Response:
    return httpx.Response(status, json={"state": "running", "outcome": "created"})


@pytest.mark.asyncio
async def test_a_running_sandbox_of_the_open_session_is_reused_without_rotation(http) -> None:
    router = http(
        {
            ("GET", "/v1/internal/hermes/sessions/current"): httpx.Response(200, json={"sessionId": SESSION_UUID}),
            ("GET", f"/hermes/sandboxes/{SESSION}"): httpx.Response(200, json={"state": "running"}),
        }
    )

    session = await ActivityEnvironment().run(hermes_activities.hermes_prepare_sandbox, "t", "org", "conv", "m")

    assert session == SESSION
    assert [method for method, *_ in router.requests] == ["GET", "GET"]
    assert router.requests[0][1].endswith("/sessions/current")


@pytest.mark.asyncio
@pytest.mark.parametrize("sandbox", [httpx.Response(404, json={}), httpx.Response(200, json={"state": "stopped"})])
async def test_a_missing_or_stopped_sandbox_gets_a_rotated_token(http, sandbox: httpx.Response) -> None:
    router = http(
        {
            ("GET", "/v1/internal/hermes/sessions/current"): httpx.Response(200, json={"sessionId": SESSION_UUID}),
            ("GET", f"/hermes/sandboxes/{SESSION}"): sandbox,
            ("POST", "/v1/internal/hermes/sessions"): httpx.Response(201, json={"sessionId": SESSION_UUID, "token": "new-token"}),
            ("POST", "/hermes/sandboxes"): _created(),
        }
    )

    session = await ActivityEnvironment().run(hermes_activities.hermes_prepare_sandbox, "t", "org", "conv", "m")

    assert session == SESSION
    assert router.requests[-1][2] == {
        "session_id": SESSION,
        "org_id": "org",
        "conversation_id": "conv",
        "token": "new-token",
        "model": "m",
    }


@pytest.mark.asyncio
async def test_without_an_open_session_one_is_opened(http) -> None:
    router = http(
        {
            ("GET", "/v1/internal/hermes/sessions/current"): httpx.Response(404, json={}),
            ("POST", "/v1/internal/hermes/sessions"): httpx.Response(201, json={"sessionId": SESSION_UUID, "token": "new"}),
            ("POST", "/hermes/sandboxes"): _created(200),
        }
    )

    assert await ActivityEnvironment().run(hermes_activities.hermes_prepare_sandbox, "t", "org", "conv", "m") == SESSION
    assert router.requests[0][0:2] == ("GET", "/v1/internal/hermes/sessions/current")


@pytest.mark.asyncio
async def test_a_refused_creation_carries_skill_executors_code(http) -> None:
    http(
        {
            ("GET", "/v1/internal/hermes/sessions/current"): httpx.Response(404, json={}),
            ("POST", "/v1/internal/hermes/sessions"): httpx.Response(201, json={"sessionId": SESSION_UUID, "token": "new"}),
            ("POST", "/hermes/sandboxes"): httpx.Response(503, json={"detail": {"code": "hermes_capacity_exhausted"}}),
        }
    )

    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(hermes_activities.hermes_prepare_sandbox, "t", "org", "conv", "m")

    assert raised.value.type == "hermes_capacity_exhausted" and raised.value.non_retryable


@pytest.mark.asyncio
async def test_binding_sends_the_turn_and_execution(http) -> None:
    router = http({("PUT", f"/v1/internal/hermes/sessions/{SESSION_UUID}/turn"): httpx.Response(204)})

    await ActivityEnvironment().run(hermes_activities.hermes_bind_turn, "t", SESSION, "turn-1", "exec-1")

    assert router.requests == [
        ("PUT", f"/v1/internal/hermes/sessions/{SESSION_UUID}/turn", {"turnId": "turn-1", "executionId": "exec-1"})
    ]


def _stream(*lines: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, content="".join(json.dumps(line) + "\n" for line in lines).encode())


@pytest.mark.asyncio
async def test_a_run_returns_only_its_bounded_outcome(http) -> None:
    router = http(
        {
            ("POST", f"/hermes/sandboxes/{SESSION}/runs"): _stream(
                {"type": "started", "run_id": "run_1"},
                {"type": "event", "event": {"event": "tool.started", "tool": "terminal"}},
                {"type": "result", "run_id": "run_1", "status": "completed", "output": "done", "usage": {}},
            )
        }
    )

    outcome = await ActivityEnvironment().run(
        hermes_activities.hermes_run_turn, SESSION, "turn-1", "conv", "hi", [{"role": "user", "content": "x"}]
    )

    assert outcome == {"status": "completed", "output": "done"}
    assert router.requests[0][2] == {
        "key": "turn-1",
        "input": "hi",
        "session_id": "conv",
        "conversation_history": [{"role": "user", "content": "x"}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lines", "outcome"),
    [
        ([{"type": "result", "status": "failed", "output": None}], {"status": "failed", "error": "hermes_run_failed"}),
        ([{"type": "result", "status": "completed", "output": "  "}], {"status": "failed", "error": "hermes_run_failed"}),
    ],
)
async def test_an_unusable_result_is_a_failed_run(http, lines: list[dict[str, Any]], outcome: dict[str, Any]) -> None:
    http({("POST", f"/hermes/sandboxes/{SESSION}/runs"): _stream(*lines)})

    assert await ActivityEnvironment().run(hermes_activities.hermes_run_turn, SESSION, "t", "c", "hi", []) == outcome


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code"),
    [
        (_stream({"type": "error", "code": "hermes_sandbox_not_found"}), "hermes_sandbox_not_found"),
        (_stream({"type": "started", "run_id": "run_1"}), "hermes_interrupted"),
        (httpx.Response(409, json={"detail": {"code": "hermes_sandbox_stopped"}}), "hermes_sandbox_stopped"),
        (httpx.Response(507, json={"detail": {"code": "hermes_workspace_full"}}), "hermes_workspace_full"),
    ],
)
async def test_run_failures_carry_a_bounded_code(http, response: httpx.Response, code: str) -> None:
    http({("POST", f"/hermes/sandboxes/{SESSION}/runs"): response})

    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(hermes_activities.hermes_run_turn, SESSION, "t", "c", "hi", [])

    assert raised.value.type == code


@pytest.mark.asyncio
async def test_a_cancelled_run_asks_hermes_to_stop(http, monkeypatch) -> None:
    stopped: list[str] = []

    async def executor(method: str, path: str, body: Any = None, timeout: float = 0) -> httpx.Response:
        stopped.append(path)
        return httpx.Response(202)

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):  # noqa: ANN204
            yield b'{"type":"started","run_id":"run_7"}\n'
            await asyncio.sleep(3600)
            yield b""

    http({("POST", f"/hermes/sandboxes/{SESSION}/runs"): httpx.Response(200, stream=Slow())})
    monkeypatch.setattr(hermes_activities, "_executor", executor)
    environment = ActivityEnvironment()
    task = asyncio.create_task(environment.run(hermes_activities.hermes_run_turn, SESSION, "t", "c", "hi", []))
    await asyncio.sleep(0.2)
    environment.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert stopped == [f"/sandboxes/{SESSION}/runs/run_7/stop"]
