import asyncio
import json
from typing import Any

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from src import hermes_activities, hermes_progress

SESSION = "0123456789abcdef0123456789abcdef"
SESSION_UUID = "01234567-89ab-cdef-0123-456789abcdef"


class Router:
    """Answers httpx requests by method and path, and records them."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(request.content) if request.content else None
        except ValueError:
            body = request.content
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
async def test_a_refused_binding_carries_business_backends_wrapped_code(http) -> None:
    """business-backend wraps an HTTPException's detail as {"error": {"message": ...}}."""
    wrapped = {"error": {"code": "FORBIDDEN", "message": {"code": "hermes_session_conflict"}, "details": {}}}
    http({("PUT", f"/v1/internal/hermes/sessions/{SESSION_UUID}/turn"): httpx.Response(409, json=wrapped)})

    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(hermes_activities.hermes_bind_turn, "t", SESSION, "turn-1", "exec-1")

    assert raised.value.type == "hermes_session_conflict" and raised.value.non_retryable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "Only runner session tokens are allowed"}},
        {"error": {"message": {"code": "https://internal.example/secret"}}},
        ["not", "an", "object"],
    ],
)
async def test_an_unknown_or_unsafe_backend_code_falls_back(http, body) -> None:
    http({("PUT", f"/v1/internal/hermes/sessions/{SESSION_UUID}/turn"): httpx.Response(403, json=body)})

    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(hermes_activities.hermes_bind_turn, "t", SESSION, "turn-1", "exec-1")

    assert raised.value.type == "hermes_backend_unavailable"


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

    assert outcome == {
        "status": "completed",
        "output": "done",
        "tool_calls": [{"call_id": "hermes-1-0", "tool": "hermes.terminal", "label": "Running commands"}],
        "tool_results": {},
    }
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
        # Hermes puts the platform's bounded model error in the failed run's text.
        (
            [{"type": "result", "status": "failed", "output": "API failed: upstream_error 402 free_request_quota_exhausted"}],
            {"status": "failed", "error": "hermes_model_unavailable"},
        ),
        (
            [{"type": "result", "status": "failed", "output": "Rate limited: upstream_error 429"}],
            {"status": "failed", "error": "hermes_model_unavailable"},
        ),
        (
            [{"type": "result", "status": "failed", "output": "upstream_error 500 after 3 retries"}],
            {"status": "failed", "error": "hermes_model_error"},
        ),
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


@pytest.mark.asyncio
async def test_a_run_publishes_safe_tool_progress_for_its_turn(http, monkeypatch) -> None:
    published: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(org_id: str, conversation_id: str, payload: dict[str, Any]) -> None:
        published.append((org_id, conversation_id, payload))

    monkeypatch.setattr(hermes_progress.publisher, "publish", publish)
    http(
        {
            ("POST", f"/hermes/sandboxes/{SESSION}/runs"): _stream(
                {"type": "started", "run_id": "run_1"},
                {"type": "event", "event": {"event": "tool.started", "tool": "terminal", "preview": "cat SECRET"}},
                {"type": "event", "event": {"event": "reasoning.available", "text": "SECRET thoughts"}},
                {"type": "event", "event": {"event": "message.delta", "delta": "SECRET answer"}},
                {"type": "event", "event": {"event": "tool.completed", "tool": "terminal", "error": True}},
                {"type": "result", "run_id": "run_1", "status": "completed", "output": "done"},
            )
        }
    )

    outcome = await ActivityEnvironment().run(
        hermes_activities.hermes_run_turn, SESSION, "turn-1", "conv", "hi", [], "org-1"
    )

    assert [(org, conv, payload["type"], payload["turn_id"]) for org, conv, payload in published] == [
        ("org-1", "conv", "tool-calls", "turn-1"),
        ("org-1", "conv", "tool-result", "turn-1"),
    ]
    assert "SECRET" not in json.dumps(published)
    assert outcome["tool_results"] == {"hermes-1-0": {"tool": "hermes.terminal", "status": "failed"}}


@pytest.mark.asyncio
async def test_preparing_keeps_the_turn_stream_open(http, monkeypatch) -> None:
    published: list[str] = []

    async def publish(org_id: str, conversation_id: str, payload: dict[str, Any]) -> None:
        published.append(payload["type"])

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.25)
        return httpx.Response(200, json={"state": "running"})

    monkeypatch.setattr(hermes_progress.publisher, "publish", publish)
    monkeypatch.setattr(hermes_progress.settings, "hermes_keepalive_seconds", 0.1)
    http(
        {
            ("GET", "/v1/internal/hermes/sessions/current"): httpx.Response(200, json={"sessionId": SESSION_UUID}),
            ("GET", f"/hermes/sandboxes/{SESSION}"): slow,
        }
    )

    await ActivityEnvironment().run(hermes_activities.hermes_prepare_sandbox, "t", "org", "conv", "m", "turn-1")

    assert published and set(published) == {"keepalive"}


@pytest.mark.asyncio
async def test_a_conversation_purge_asks_skill_executor(http) -> None:
    router = http(
        {("DELETE", "/hermes/conversations/org-1/conv-1"): httpx.Response(200, json={"sandboxes": 1, "snapshots": 3})}
    )

    counts = await ActivityEnvironment().run(hermes_activities.hermes_purge_conversation, "org-1", "conv-1")

    assert counts == {"sandboxes": 1, "snapshots": 3} and router.requests[0][0] == "DELETE"


@pytest.mark.asyncio
async def test_a_failed_conversation_purge_carries_skill_executors_code(http) -> None:
    http(
        {
            ("DELETE", "/hermes/conversations/org-1/conv-1"): httpx.Response(
                503, json={"detail": {"code": "hermes_snapshot_store_failed"}}
            )
        }
    )
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(hermes_activities.hermes_purge_conversation, "org-1", "conv-1")
    assert raised.value.type == "hermes_snapshot_store_failed"


@pytest.mark.asyncio
async def test_memory_is_written_in_and_only_its_versions_and_hashes_come_back(http) -> None:
    router = http(
        {
            ("GET", "/v1/internal/hermes/memory"): httpx.Response(
                200,
                json={"memory": {"content": "repo uses uv", "version": 3}, "user": {"content": "", "version": 0}},
            ),
            ("PUT", f"/hermes/sandboxes/{SESSION}/memory"): httpx.Response(204),
        }
    )

    loaded = await ActivityEnvironment().run(hermes_activities.hermes_load_memory, "t", SESSION)

    assert router.requests[1][2] == {"memory": "repo uses uv", "user": ""}
    assert "repo uses uv" not in json.dumps(loaded)
    assert loaded["memory"]["version"] == 3 and loaded["user"]["version"] == 0


@pytest.mark.asyncio
async def test_only_changed_memory_is_stored_on_the_version_it_was_read_at(http) -> None:
    import hashlib

    same = hashlib.sha256(b"repo uses uv").hexdigest()
    router = http(
        {
            ("GET", f"/hermes/sandboxes/{SESSION}/memory"): httpx.Response(
                200, json={"memory": "repo uses uv", "user": "likes tea"}
            ),
            ("PUT", "/v1/internal/hermes/memory"): httpx.Response(409, json={"detail": {"code": "hermes_memory_conflict"}}),
        }
    )
    loaded = {"memory": {"version": 3, "sha256": same}, "user": {"version": 0, "sha256": "other"}}

    outcome = await ActivityEnvironment().run(hermes_activities.hermes_save_memory, "t", SESSION, loaded)

    assert outcome == {"memory": "unchanged", "user": "conflict"}
    [(_, _, body)] = [request for request in router.requests if request[0] == "PUT"]
    assert body == {"kind": "user", "content": "likes tea", "baseVersion": 0}


@pytest.mark.asyncio
async def test_unreadable_memory_files_are_not_stored(http) -> None:
    http({("GET", f"/hermes/sandboxes/{SESSION}/memory"): httpx.Response(200, json={"memory": None, "user": None})})

    outcome = await ActivityEnvironment().run(
        hermes_activities.hermes_save_memory, "t", SESSION, {"memory": {"version": 1, "sha256": "x"}}
    )

    assert outcome == {"memory": "unreadable"}


@pytest.mark.asyncio
async def test_skills_are_submitted_as_drafts_and_only_counts_come_back(http) -> None:
    files = [{"path": "SKILL.md", "content": "---\nname: mine\ndescription: d\n---\nsecret steps"}]
    outcomes = iter(
        [
            httpx.Response(200, json={"outcome": "drafted"}),
            httpx.Response(200, json={"outcome": "unchanged"}),
            httpx.Response(422, json={"detail": {"code": "hermes_skill_invalid"}}),
        ]
    )
    router = http(
        {
            ("GET", f"/hermes/sandboxes/{SESSION}/skills"): httpx.Response(
                200,
                json={"skills": [{"path": name, "files": files} for name in ("a", "b", "c")], "skipped": 2},
            ),
            ("PUT", "/v1/internal/hermes/skill-drafts"): lambda request: next(outcomes),
        }
    )

    counts = await ActivityEnvironment().run(hermes_activities.hermes_save_skills, "t", SESSION)

    assert counts == {"drafted": 1, "updated": 0, "unchanged": 1, "refused": 1, "skipped": 2}
    assert [request[2] for request in router.requests if request[0] == "PUT"] == [
        {"path": name, "files": files} for name in ("a", "b", "c")
    ]
    assert "secret steps" not in json.dumps(counts)


@pytest.mark.asyncio
async def test_platform_skills_are_hydrated_as_the_backend_lists_them(http) -> None:
    router = http(
        {
            ("GET", "/v1/internal/hermes/platform-skills"): httpx.Response(
                200,
                json={"skills": [{"skillId": "s", "dir": "standup-1a", "bundleStorageKey": "skills/k.zip", "bundleChecksum": "c" * 64}]},
            ),
            ("PUT", f"/hermes/sandboxes/{SESSION}/platform-skills"): httpx.Response(200, json={"hydrated": 1, "skipped": 0}),
        }
    )

    counts = await ActivityEnvironment().run(hermes_activities.hermes_hydrate_skills, "t", SESSION)

    assert counts == {"hydrated": 1, "skipped": 0}
    assert router.requests[1][2] == {
        "skills": [{"dir": "standup-1a", "bundle_storage_key": "skills/k.zip", "bundle_checksum": "c" * 64}]
    }


@pytest.mark.asyncio
async def test_hydration_fails_closed_by_clearing_the_platform_skills(http) -> None:
    written: list[Any] = []

    def put(request: httpx.Request) -> httpx.Response:
        written.append(json.loads(request.content)["skills"])
        return httpx.Response(502, json={"detail": {"code": "hermes_skills_failed"}}) if len(written) == 1 else httpx.Response(
            200, json={"hydrated": 0, "skipped": 0}
        )

    http(
        {
            ("GET", "/v1/internal/hermes/platform-skills"): httpx.Response(
                200, json={"skills": [{"dir": "a", "bundleStorageKey": "k", "bundleChecksum": "c" * 64}]}
            ),
            ("PUT", f"/hermes/sandboxes/{SESSION}/platform-skills"): put,
        }
    )

    counts = await ActivityEnvironment().run(hermes_activities.hermes_hydrate_skills, "t", SESSION)

    assert counts == {"hydrated": 0, "skipped": 0}
    assert [len(item) for item in written] == [1, 0]


@pytest.mark.asyncio
async def test_without_the_list_the_platform_skills_are_cleared_and_a_failed_clear_stops_the_turn(http) -> None:
    http(
        {
            ("GET", "/v1/internal/hermes/platform-skills"): httpx.Response(403, json={"detail": {"code": "hermes_turn_inactive"}}),
            ("PUT", f"/hermes/sandboxes/{SESSION}/platform-skills"): httpx.Response(
                502, json={"detail": {"code": "hermes_skills_failed"}}
            ),
        }
    )

    with pytest.raises(ApplicationError) as failed:
        await ActivityEnvironment().run(hermes_activities.hermes_hydrate_skills, "t", SESSION)

    assert failed.value.type == "hermes_skills_failed"


@pytest.mark.asyncio
async def test_a_workflow_node_turn_is_bound_with_its_run_phase_and_node(http) -> None:
    router = http({("PUT", f"/v1/internal/hermes/sessions/{SESSION_UUID}/turn"): httpx.Response(204)})

    await ActivityEnvironment().run(
        hermes_activities.hermes_bind_turn, "t", SESSION, "turn-1", "exec-1", "run-9", "node", "research"
    )
    await ActivityEnvironment().run(hermes_activities.hermes_bind_turn, "t", SESSION, "turn-2", "exec-2")

    assert [request[2] for request in router.requests] == [
        {"turnId": "turn-1", "executionId": "exec-1", "workflowRunId": "run-9", "workflowPhase": "node", "workflowNodeKey": "research"},
        {"turnId": "turn-2", "executionId": "exec-2"},
    ]


@pytest.mark.asyncio
async def test_closing_with_an_expired_runner_token_leaves_the_session_to_the_backend(http) -> None:
    http({("POST", f"/v1/internal/hermes/sessions/{SESSION_UUID}/close"): httpx.Response(401)})

    # No failure, so no retries: the backend closes sessions left open.
    await ActivityEnvironment().run(hermes_activities.hermes_close_session, "expired", SESSION)


@pytest.mark.asyncio
async def test_closing_still_fails_when_the_backend_is_down(http) -> None:
    http({("POST", f"/v1/internal/hermes/sessions/{SESSION_UUID}/close"): httpx.Response(503)})

    with pytest.raises(ApplicationError):
        await ActivityEnvironment().run(hermes_activities.hermes_close_session, "t", SESSION)


INPUT_ID = "0b7c5a4e-1d2f-4a3b-9c8d-7e6f5a4b3c2d"


def _inputs(*items: tuple[str, bytes]) -> httpx.Response:
    import hashlib

    return httpx.Response(
        200,
        json={
            "inputs": [
                {"fileId": f"{INPUT_ID[:-1]}{index}", "name": name, "sizeBytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for index, (name, data) in enumerate(items)
            ]
        },
    )


@pytest.mark.asyncio
async def test_inputs_missing_or_changed_in_the_sandbox_are_written_after_a_check(http) -> None:
    import hashlib

    sales, notes = b"PK sales workbook", b"# Notes\n"
    router = http(
        {
            ("GET", "/v1/internal/hermes/inputs"): _inputs(("sales.xlsx", sales), ("notes.md", notes)),
            ("GET", f"/hermes/sandboxes/{SESSION}/inputs"): httpx.Response(
                200, json={"files": {"notes.md": hashlib.sha256(notes).hexdigest(), "sales.xlsx": "0" * 64}}
            ),
            ("GET", f"/v1/internal/hermes/inputs/{INPUT_ID[:-1]}0/content"): httpx.Response(200, content=sales),
            ("PUT", f"/hermes/sandboxes/{SESSION}/inputs"): httpx.Response(204),
        }
    )

    counts = await ActivityEnvironment().run(hermes_activities.hermes_sync_inputs, "t", SESSION)

    assert counts == {"written": 1, "kept": 1}
    [put] = [request for request in router.requests if request[0] == "PUT"]
    assert put[2] == sales


@pytest.mark.asyncio
async def test_inputs_whose_bytes_do_not_match_their_record_fail_the_turn(http) -> None:
    http(
        {
            ("GET", "/v1/internal/hermes/inputs"): _inputs(("sales.xlsx", b"PK sales workbook")),
            ("GET", f"/hermes/sandboxes/{SESSION}/inputs"): httpx.Response(200, json={"files": {}}),
            ("GET", f"/v1/internal/hermes/inputs/{INPUT_ID[:-1]}0/content"): httpx.Response(200, content=b"tampered"),
        }
    )

    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(hermes_activities.hermes_sync_inputs, "t", SESSION)

    assert raised.value.type == "hermes_inputs_unavailable"


@pytest.mark.asyncio
async def test_a_conversation_without_inputs_never_asks_the_sandbox(http) -> None:
    router = http({("GET", "/v1/internal/hermes/inputs"): httpx.Response(200, json={"inputs": []})})

    assert await ActivityEnvironment().run(hermes_activities.hermes_sync_inputs, "t", SESSION) == {
        "written": 0,
        "kept": 0,
    }
    assert len(router.requests) == 1
