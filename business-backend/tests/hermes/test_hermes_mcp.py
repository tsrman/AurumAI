"""The MCP bridge: a fixed catalogue, a handshake between turns, and tool calls
authorized live as operations of the session's bound execution."""

import json
import uuid
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from src.core.capability_catalog import Capability
from src.core.config import settings
from src.models.conversation import Conversation
from src.services import hermes_mcp
from src.services.image_tools import ImageToolExecutionError
from src.services.web_tools import WebToolExecutionError


async def _rpc(env, token: str | None, message: Any, *, raw: bytes | None = None) -> httpx.Response:  # noqa: ANN001
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(transport=ASGITransport(app=env.app), base_url="http://test") as client:
        if raw is not None:
            return await client.post("/v1/hermes/mcp", content=raw, headers=headers)
        return await client.post("/v1/hermes/mcp", json=message, headers=headers)


def _request(method: str, params: dict[str, Any] | None = None, request_id: Any = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _call(name: str, arguments: Any, request_id: Any = 7) -> dict[str, Any]:
    return _request("tools/call", {"name": name, "arguments": arguments}, request_id)


def _tool_payload(response: httpx.Response) -> tuple[bool, dict[str, Any]]:
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    [content] = result["content"]
    assert content["type"] == "text"
    return result["isError"], json.loads(content["text"])


@pytest.fixture
def both_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WEB_ENABLED", True)
    monkeypatch.setattr(settings, "IMAGES_ENABLED", True)


class RecordingService:
    def __init__(self, result: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result or {"ok": True}
        self.error = error

    async def execute(self, session, subject, execution_id, invocation_id, tool, args):  # noqa: ANN001, ANN201
        self.calls.append(
            {"execution_id": execution_id, "invocation_id": invocation_id, "tool": tool, "args": args}
        )
        if self.error is not None:
            raise self.error
        return self.result


async def test_the_handshake_and_listing_work_between_turns(hermes_env) -> None:
    opened = await hermes_env.open_session(bind=False)

    initialized = await _rpc(
        hermes_env, opened["token"], _request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
    )
    notified = await _rpc(hermes_env, opened["token"], {"jsonrpc": "2.0", "method": "notifications/initialized"})
    pinged = await _rpc(hermes_env, opened["token"], _request("ping", request_id="p"))
    listed = await _rpc(hermes_env, opened["token"], _request("tools/list", request_id=2))

    assert initialized.status_code == 200
    body = initialized.json()
    assert body["id"] == 1 and body["result"]["protocolVersion"] == "2025-06-18"
    assert body["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert "mcp-session-id" not in {name.lower() for name in initialized.headers}
    assert notified.status_code == 202 and notified.content == b""
    assert pinged.json() == {"jsonrpc": "2.0", "id": "p", "result": {}}
    assert [tool["name"] for tool in listed.json()["result"]["tools"]] == [
        "knowledge_search",
        "attach_file",
        "list_skills",
        "run_skill",
    ]


async def test_an_unknown_protocol_version_gets_the_latest(hermes_env) -> None:
    opened = await hermes_env.open_session(bind=False)

    response = await _rpc(hermes_env, opened["token"], _request("initialize", {"protocolVersion": "1999-01-01"}))

    assert response.json()["result"]["protocolVersion"] == "2025-11-25"


async def test_the_catalogue_follows_the_operator_switches(hermes_env, both_enabled) -> None:
    opened = await hermes_env.open_session(bind=False)

    listed = await _rpc(hermes_env, opened["token"], _request("tools/list"))

    tools = listed.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == [
        "knowledge_search",
        "web_search",
        "web_read",
        "generate_image",
        "edit_image",
        "attach_file",
        "list_skills",
        "run_skill",
    ]
    search = tools[1]["inputSchema"]
    assert search["type"] == "object" and search["required"] == ["query"]
    assert search["additionalProperties"] is False


async def test_a_revoked_or_invalid_token_cannot_list(hermes_env) -> None:
    opened = await hermes_env.open_session(bind=False)
    await hermes_env.internal("POST", f"/sessions/{opened['sessionId']}/close")

    revoked = await _rpc(hermes_env, opened["token"], _request("tools/list"))
    invalid = await _rpc(hermes_env, "not-a-token", _request("tools/list"))
    missing = await _rpc(hermes_env, None, _request("tools/list"))

    assert revoked.status_code == 403 and "hermes_session_revoked" in revoked.text
    assert (invalid.status_code, missing.status_code) == (401, 401)


async def test_the_endpoint_is_absent_while_the_operator_switch_is_off(hermes_env, monkeypatch) -> None:
    opened = await hermes_env.open_session(bind=False)
    monkeypatch.setattr(settings, "HERMES_ENABLED", False)

    response = await _rpc(hermes_env, opened["token"], _request("tools/list"))

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("raw", "status", "code"),
    [
        (b"{not json", 400, -32700),
        (b"[]", 400, -32600),
        (b'{"jsonrpc":"1.0","id":1,"method":"ping"}', 400, -32600),
        (b'{"jsonrpc":"2.0","id":true,"method":"ping"}', 400, -32600),
        (b'{"jsonrpc":"2.0","id":1,"method":"ping","params":[]}', 200, -32602),
        (b'{"jsonrpc":"2.0","id":1,"method":"resources/list"}', 200, -32601),
        (b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"shell"}}', 200, -32602),
        (b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"web_read","arguments":[]}}', 200, -32602),
    ],
)
async def test_malformed_messages_get_json_rpc_errors(hermes_env, raw: bytes, status: int, code: int) -> None:
    opened = await hermes_env.open_session()

    response = await _rpc(hermes_env, opened["token"], None, raw=raw)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


async def test_an_oversized_message_is_refused(hermes_env) -> None:
    opened = await hermes_env.open_session()

    response = await _rpc(hermes_env, opened["token"], _call("web_read", {"url": "x" * (1024 * 1024)}))

    assert response.status_code == 413


async def test_a_call_between_turns_is_a_tool_error(hermes_env, both_enabled, monkeypatch) -> None:
    web = RecordingService()
    monkeypatch.setattr(hermes_mcp, "web_tools_service", web)
    opened = await hermes_env.open_session()
    await hermes_env.internal("DELETE", f"/sessions/{opened['sessionId']}/turn")

    is_error, payload = _tool_payload(await _rpc(hermes_env, opened["token"], _call("web_search", {"query": "x"})))

    assert is_error and payload == {"error": "hermes_turn_inactive"}
    assert web.calls == []


@pytest.mark.parametrize("capability", [Capability.AGENTS_HERMES, Capability.CHAT_SEND])
async def test_live_capability_revocation_denies_the_next_call(
    hermes_env, both_enabled, monkeypatch, capability: Capability
) -> None:
    monkeypatch.setattr(hermes_mcp, "web_tools_service", RecordingService())
    opened = await hermes_env.open_session()
    await hermes_env.revoke(capability)

    is_error, payload = _tool_payload(await _rpc(hermes_env, opened["token"], _call("web_search", {"query": "x"})))

    assert is_error and payload == {"error": "hermes_access_denied"}


async def test_web_calls_are_operations_of_the_bound_execution(hermes_env, both_enabled, monkeypatch) -> None:
    web = RecordingService({"results": []})
    monkeypatch.setattr(hermes_mcp, "web_tools_service", web)
    opened = await hermes_env.open_session()
    token = opened["token"]

    first = _tool_payload(await _rpc(hermes_env, token, _call("web_search", {"query": "x"}, 7)))
    retried = _tool_payload(await _rpc(hermes_env, token, _call("web_search", {"query": "x"}, 7)))
    reused_id = _tool_payload(await _rpc(hermes_env, token, _call("web_search", {"query": "y"}, 7)))
    read = _tool_payload(await _rpc(hermes_env, token, _call("web_read", {"url": "https://example.org"}, "r")))

    assert first == retried == reused_id == read == (False, {"results": []})
    assert {call["execution_id"] for call in web.calls} == {hermes_env.execution_id}
    invocations = [call["invocation_id"] for call in web.calls]
    assert invocations[0] == invocations[1]
    assert len(set(invocations)) == 3
    assert web.calls[0]["args"] == {"query": "x", "language": None, "time_range": None}
    assert web.calls[3]["tool"] == "web_read"


async def test_another_session_never_shares_an_operation(hermes_env, both_enabled, monkeypatch) -> None:
    web = RecordingService()
    monkeypatch.setattr(hermes_mcp, "web_tools_service", web)
    first = await hermes_env.open_session()
    await hermes_env.internal("POST", f"/sessions/{first['sessionId']}/close")
    second = await hermes_env.open_session()

    for opened in (first, second):
        await _rpc(hermes_env, opened["token"], _call("web_search", {"query": "x"}))

    [call] = web.calls
    assert call["invocation_id"] == hermes_mcp.invocation_id(
        uuid.UUID(second["sessionId"]), 7, "web_search", {"query": "x"}
    )


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (WebToolExecutionError("web_limit"), "web_limit"),
        (WebToolExecutionError("web_access_denied"), "web_access_denied"),
        (WebToolExecutionError("interaction_conflict"), "hermes_turn_inactive"),
        (WebToolExecutionError("something_internal"), "web_unavailable"),
    ],
)
async def test_web_failures_are_bounded_tool_errors(
    hermes_env, both_enabled, monkeypatch, error: Exception, code: str
) -> None:
    monkeypatch.setattr(hermes_mcp, "web_tools_service", RecordingService(error=error))
    opened = await hermes_env.open_session()

    is_error, payload = _tool_payload(await _rpc(hermes_env, opened["token"], _call("web_read", {"url": "https://a.example"})))

    assert is_error and payload == {"error": code}


async def test_image_calls_reach_the_image_service(hermes_env, both_enabled, monkeypatch) -> None:
    images = RecordingService({"imageId": str(uuid.uuid4()), "contentType": "image/png"})
    monkeypatch.setattr(hermes_mcp, "image_tools_service", images)
    opened = await hermes_env.open_session()
    image_id = str(uuid.uuid4())

    generated = _tool_payload(await _rpc(hermes_env, opened["token"], _call("generate_image", {"prompt": "a cat"})))
    edited = _tool_payload(
        await _rpc(hermes_env, opened["token"], _call("edit_image", {"image_id": image_id, "prompt": "hat"}, 8))
    )

    assert not generated[0] and not edited[0]
    assert [call["tool"] for call in images.calls] == ["generate_image", "edit_image"]
    assert images.calls[1]["args"] == {"prompt": "hat", "size": "auto", "image_id": image_id}
    assert {call["execution_id"] for call in images.calls} == {hermes_env.execution_id}


async def test_image_failures_and_switches_are_tool_errors(hermes_env, monkeypatch) -> None:
    monkeypatch.setattr(
        hermes_mcp, "image_tools_service", RecordingService(error=ImageToolExecutionError("image_quota_exceeded"))
    )
    opened = await hermes_env.open_session()

    disabled = _tool_payload(await _rpc(hermes_env, opened["token"], _call("generate_image", {"prompt": "a"})))
    monkeypatch.setattr(settings, "IMAGES_ENABLED", True)
    exceeded = _tool_payload(await _rpc(hermes_env, opened["token"], _call("generate_image", {"prompt": "a"}, 9)))

    assert disabled == (True, {"error": "image_disabled"})
    assert exceeded == (True, {"error": "image_quota_exceeded"})


@pytest.mark.parametrize(
    "arguments",
    [{"query": ""}, {"query": "x", "limit": 11}, {"query": "x", "extra": 1}, {"query": 5}],
)
async def test_invalid_arguments_are_a_tool_error(hermes_env, arguments: dict[str, Any]) -> None:
    opened = await hermes_env.open_session()

    is_error, payload = _tool_payload(await _rpc(hermes_env, opened["token"], _call("knowledge_search", arguments)))

    assert is_error and payload == {"error": "invalid_arguments"}


async def test_an_unexpected_failure_is_a_tool_error(hermes_env, both_enabled, monkeypatch) -> None:
    monkeypatch.setattr(hermes_mcp, "web_tools_service", RecordingService(error=RuntimeError("boom")))
    opened = await hermes_env.open_session()

    is_error, payload = _tool_payload(await _rpc(hermes_env, opened["token"], _call("web_search", {"query": "x"})))

    assert is_error and payload == {"error": "tool_unavailable"}


async def test_knowledge_search_without_attachments_finds_nothing(hermes_env) -> None:
    opened = await hermes_env.open_session()

    is_error, payload = _tool_payload(await _rpc(hermes_env, opened["token"], _call("knowledge_search", {"query": "x"})))

    assert not is_error and payload["results"] == []


async def _attach(env) -> uuid.UUID:  # noqa: ANN001
    from src.services.conversation_attachments import _get_or_create_conversation_knowledge_base

    async with env.sessions() as db:
        conversation = await db.scalar(select(Conversation).where(Conversation.id == env.conversation_id))
        assert conversation is not None
        knowledge_base = await _get_or_create_conversation_knowledge_base(db, conversation)
        return knowledge_base.id


async def test_knowledge_search_covers_only_the_conversations_attachments(hermes_env, monkeypatch) -> None:
    knowledge_base_id = await _attach(hermes_env)
    searched: list[tuple[Any, ...]] = []

    async def search(org_id, kb_id, *, query, limit):  # noqa: ANN001, ANN202
        searched.append((org_id, kb_id, query, limit))
        return {
            "results": [
                {"content": "y" * 5000, "relevance_score": 0.9, "document_id": "hidden"},
                {"content": "z", "relevance_score": "high"},
                {"content": "w", "relevance_score": 0.1},
            ],
            "warnings": ["reranker down"],
        }

    monkeypatch.setattr(hermes_mcp.rag_client, "search", search)
    opened = await hermes_env.open_session()

    is_error, payload = _tool_payload(
        await _rpc(hermes_env, opened["token"], _call("knowledge_search", {"query": "q", "limit": 2}))
    )

    assert not is_error
    assert searched == [(str(hermes_env.org_id), str(knowledge_base_id), "q", 2)]
    assert payload == {
        "results": [
            {"content": "y" * 4000, "relevanceScore": 0.9},
            {"content": "z", "relevanceScore": None},
        ],
        "warnings": ["Search completed with limitations"],
    }


async def test_knowledge_search_needs_the_users_live_grant(hermes_env, monkeypatch) -> None:
    from sqlalchemy import delete

    from src.models.access_grant import AccessGrant

    knowledge_base_id = await _attach(hermes_env)
    async with hermes_env.sessions() as db:
        await db.execute(delete(AccessGrant).where(AccessGrant.resource_id == str(knowledge_base_id)))
        await db.commit()

    async def search(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("searched without a grant")

    monkeypatch.setattr(hermes_mcp.rag_client, "search", search)
    opened = await hermes_env.open_session()

    is_error, payload = _tool_payload(await _rpc(hermes_env, opened["token"], _call("knowledge_search", {"query": "q"})))

    assert is_error and payload == {"error": "knowledge_access_denied"}


async def test_a_failed_search_service_is_a_tool_error(hermes_env, monkeypatch) -> None:
    from fastapi import HTTPException

    await _attach(hermes_env)

    async def search(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise HTTPException(status_code=503, detail="RAG service unavailable: secret detail")

    monkeypatch.setattr(hermes_mcp.rag_client, "search", search)
    opened = await hermes_env.open_session()

    response = await _rpc(hermes_env, opened["token"], _call("knowledge_search", {"query": "q"}))

    assert _tool_payload(response) == (True, {"error": "knowledge_unavailable"})
    assert "secret detail" not in response.text


async def test_a_web_search_runs_as_a_durable_operation_of_the_turn(hermes_env, both_enabled, monkeypatch) -> None:
    from src.models.interaction import InteractionTurn, ToolOperation
    from src.services import web_tools
    from src.services.interaction import interaction_service

    class FakeWebClient:
        def __init__(self) -> None:
            self.requests: list[tuple[str, dict[str, Any]]] = []

        async def execute(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
            self.requests.append((tool, args))
            return {
                "results": [
                    {"title": "T", "url": "https://example.org/a", "snippet": "s", "published_at": None}
                ]
            }

    async def allow_model(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        return None

    client = FakeWebClient()
    monkeypatch.setattr(hermes_mcp, "web_tools_service", web_tools.WebToolsService(client=client))
    monkeypatch.setattr(interaction_service, "_model_authorizer", allow_model)
    await hermes_env.grant(Capability.WEB_ACCESS)
    opened = await hermes_env.open_session()

    first = _tool_payload(await _rpc(hermes_env, opened["token"], _call("web_search", {"query": "x"})))
    retried = _tool_payload(await _rpc(hermes_env, opened["token"], _call("web_search", {"query": "x"})))

    assert not first[0] and first == retried
    [hit] = first[1]["results"]
    assert hit["url"] == "https://example.org/a" and uuid.UUID(hit["sourceId"])
    assert len(client.requests) == 1
    async with hermes_env.sessions() as db:
        [operation] = (
            await db.scalars(select(ToolOperation).where(ToolOperation.org_id == hermes_env.org_id))
        ).all()
        turn = await db.get(InteractionTurn, hermes_env.turn_id)
    assert operation.execution_id == hermes_env.execution_id
    assert operation.invocation_id == hermes_mcp.invocation_id(
        uuid.UUID(opened["sessionId"]), 7, "web_search", {"query": "x"}
    )
    assert turn is not None and turn.search_count == 1
