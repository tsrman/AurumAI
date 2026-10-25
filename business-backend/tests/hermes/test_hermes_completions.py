"""Hermes native-tools completions: boundary, live gates, streaming and images."""

import base64
import json
import struct
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport

from src.core.capability_catalog import Capability
from src.core.config import settings
from src.services.hermes_session_token import hermes_session_token_service
from src.services.image_uploads import OMITTED_IMAGE_NOTE
from src.services.session_token import session_token_service

from tests.hermes.conftest import MODEL, scripted_stream

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }
]
TOOL_CALL_CHUNKS = [
    json.dumps(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "web_search", "arguments": '{"q":'},
                            }
                        ]
                    },
                }
            ]
        }
    ),
    json.dumps(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"x"}'}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        }
    ),
    json.dumps(
        {
            "choices": [],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        }
    ),
]


def _png(width: int = 2, height: int = 2) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"


def _image_part(data: bytes, mime: str = "image/png") -> dict[str, object]:
    encoded = base64.b64encode(data).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def _token(env, *, generation: int = 1, now: datetime | None = None) -> str:  # noqa: ANN001
    return hermes_session_token_service.issue(
        org_id=env.org_id,
        user_id=env.user_id,
        conversation_id=env.conversation_id,
        generation=generation,
        now=now,
    ).token


def _body(**overrides: object) -> dict[str, object]:
    return {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are Hermes."},
            {"role": "user", "content": "Find x"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_0",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": '{"q":"x"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_0", "content": "results"},
        ],
        "tools": TOOLS,
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": False},
        "user": "ignored-unknown-field",
        **overrides,
    }


async def _post(env, token: str | None, body: dict[str, object]) -> httpx.Response:  # noqa: ANN001
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(transport=ASGITransport(app=env.app), base_url="http://test") as client:
        return await client.post("/v1/hermes/chat/completions", json=body, headers=headers)


async def test_streams_native_tool_calls_unchanged_with_live_checks_and_quota(hermes_env) -> None:
    requests: list[dict[str, object]] = []
    with patch(
        "src.services.completion_stream.llm_client.stream_chat",
        side_effect=scripted_stream(TOOL_CALL_CHUNKS, requests),
    ):
        response = await _post(hermes_env, _token(hermes_env), _body())

    assert response.status_code == 200
    data_lines = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    assert data_lines == [*TOOL_CALL_CHUNKS, "[DONE]"]
    [upstream] = requests
    assert upstream["tools"] == TOOLS
    assert upstream["tool_choice"] == "auto"
    assert upstream["stream"] is True
    assert upstream["stream_options"] == {"include_usage": True}
    assert "user" not in upstream
    assert upstream["messages"][2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_0",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"q":"x"}'},
            }
        ],
    }
    assert upstream["messages"][3] == {"role": "tool", "content": "results", "tool_call_id": "call_0"}
    [reservation] = hermes_env.quota.reservations
    assert reservation["conversation_id"] == hermes_env.conversation_id
    assert reservation["model"] == MODEL
    assert set(reservation) == {"org_id", "user_id", "conversation_id", "model", "request_id"}
    assert hermes_env.quota.completed == [(reservation["request_id"], 11, 7, 18)]
    [(org, model, _)] = hermes_env.granted_models
    assert (org, model) == (str(hermes_env.org_id), MODEL)


async def test_workflow_correlation_in_the_body_is_never_billed(hermes_env) -> None:
    requests: list[dict[str, object]] = []
    body = _body(workflow_run_id="00000000-0000-0000-0000-000000000001", workflow_phase="node")
    with patch(
        "src.services.completion_stream.llm_client.stream_chat",
        side_effect=scripted_stream(TOOL_CALL_CHUNKS, requests),
    ):
        response = await _post(hermes_env, _token(hermes_env), body)

    assert response.status_code == 200
    assert "workflow_run_id" not in hermes_env.quota.reservations[0]
    assert "workflow_run_id" not in requests[0]


async def test_route_is_absent_while_the_operator_switch_is_off(hermes_env, monkeypatch) -> None:
    monkeypatch.setattr(settings, "HERMES_ENABLED", False)

    response = await _post(hermes_env, _token(hermes_env), _body())

    assert response.status_code == 404


@pytest.mark.parametrize("kind", ["missing", "runner", "garbage"])
async def test_only_hermes_session_tokens_are_accepted(hermes_env, kind: str) -> None:
    token = {
        "missing": None,
        "runner": session_token_service.issue(
            str(hermes_env.conversation_id), str(hermes_env.user_id), str(hermes_env.org_id)
        ),
        "garbage": "not-a-token",
    }[kind]

    response = await _post(hermes_env, token, _body())

    assert response.status_code == 401


async def test_expired_token_is_rejected(hermes_env) -> None:
    old = datetime.now(timezone.utc) - timedelta(seconds=settings.HERMES_SESSION_TOKEN_TTL_SECONDS + 5)

    response = await _post(hermes_env, _token(hermes_env, now=old), _body())

    assert response.status_code == 401
    assert "expired" in response.text


@pytest.mark.parametrize("capability", [Capability.AGENTS_HERMES, Capability.CHAT_SEND])
async def test_live_capability_revocation_denies_the_next_call(hermes_env, capability) -> None:
    token = _token(hermes_env)
    await hermes_env.revoke(capability)

    response = await _post(hermes_env, token, _body())

    assert response.status_code == 403
    assert "hermes_access_denied" in response.text
    assert hermes_env.quota.reservations == []


async def test_token_is_useless_between_turns(hermes_env) -> None:
    token = _token(hermes_env)
    await hermes_env.finish_turn()

    response = await _post(hermes_env, token, _body())

    assert response.status_code == 403
    assert "hermes_turn_inactive" in response.text
    assert hermes_env.quota.reservations == []


async def test_stale_conversation_generation_is_fenced(hermes_env) -> None:
    response = await _post(hermes_env, _token(hermes_env, generation=2), _body())

    assert response.status_code in (404, 409)
    assert hermes_env.quota.reservations == []


@pytest.mark.parametrize(
    "message",
    [
        {"role": "tool", "content": "orphan result"},
        {"role": "user", "content": None},
        {
            "role": "user",
            "content": "x",
            "tool_calls": [
                {"id": "c", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}
            ],
        },
        {"role": "assistant", "content": [_image_part(_png())]},
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "https://attacker.example/a.png"}}],
        },
    ],
)
async def test_malformed_or_unsafe_messages_are_rejected(hermes_env, message) -> None:
    response = await _post(hermes_env, _token(hermes_env), _body(messages=[message]))

    assert response.status_code == 400
    assert hermes_env.quota.reservations == []


async def test_tools_are_bounded_and_unique(hermes_env, monkeypatch) -> None:
    monkeypatch.setattr(settings, "HERMES_MAX_TOOLS", 1)
    two = [TOOLS[0], {**TOOLS[0], "function": {**TOOLS[0]["function"], "name": "other"}}]

    too_many = await _post(hermes_env, _token(hermes_env), _body(tools=two))
    duplicate = await _post(hermes_env, _token(hermes_env), _body(tools=[TOOLS[0], TOOLS[0]]))

    assert too_many.status_code == 400
    assert duplicate.status_code == 400


async def test_invalid_image_bytes_are_rejected(hermes_env) -> None:
    body = _body(messages=[{"role": "user", "content": [_image_part(b"not an image")]}])

    response = await _post(hermes_env, _token(hermes_env), body)

    assert response.status_code == 422
    assert "hermes_invalid_image" in response.text


async def test_image_declared_with_another_type_is_rejected(hermes_env) -> None:
    body = _body(messages=[{"role": "user", "content": [_image_part(_png(), "image/webp")]}])

    response = await _post(hermes_env, _token(hermes_env), body)

    assert response.status_code == 422


@pytest.mark.parametrize("vision", [False, True])
async def test_images_reach_only_vision_models_and_only_the_newest(
    hermes_env, monkeypatch, vision: bool
) -> None:
    async def vision_enabled(session, org_id, model):  # noqa: ANN001, ANN202
        return vision

    monkeypatch.setattr("src.routers.hermes.vision_enabled", vision_enabled)
    monkeypatch.setattr("src.services.hermes_completions.MAX_VISION_IMAGES_PER_REQUEST", 1)
    older, newer = _image_part(_png(1, 1)), _image_part(_png(3, 3))
    body = _body(
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "first"}, older]},
            {"role": "user", "content": [newer]},
        ]
    )
    requests: list[dict[str, object]] = []
    with patch(
        "src.services.completion_stream.llm_client.stream_chat",
        side_effect=scripted_stream(TOOL_CALL_CHUNKS, requests),
    ):
        response = await _post(hermes_env, _token(hermes_env), body)

    assert response.status_code == 200
    first, second = requests[0]["messages"]
    note = {"type": "text", "text": OMITTED_IMAGE_NOTE}
    assert first["content"] == [{"type": "text", "text": "first"}, note]
    assert second["content"] == ([newer] if vision else [note])


async def test_replayed_assistant_extras_are_dropped_not_rejected(hermes_env) -> None:
    body = _body(
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello", "refusal": None, "reasoning_content": "hidden"},
            {"role": "user", "content": "again"},
        ]
    )
    requests: list[dict[str, object]] = []
    with patch(
        "src.services.completion_stream.llm_client.stream_chat",
        side_effect=scripted_stream(TOOL_CALL_CHUNKS, requests),
    ):
        response = await _post(hermes_env, _token(hermes_env), body)

    assert response.status_code == 200
    assert requests[0]["messages"][1] == {"role": "assistant", "content": "hello"}
