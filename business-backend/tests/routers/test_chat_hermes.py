"""Choosing Hermes Agent in chat: the operator switch, the capabilities, the
organization's Hermes model, and a turn signalled to the runner as Hermes."""

import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.routers import chat
from src.schemas.chat import ChatRequest
from src.services.hermes_access import HermesAccessDenied
from tests.routers.test_chat_workflow_runtime import AllowQuota, _fixture, _image_stream_patches

pytestmark = pytest.mark.usefixtures("mock_chat_lifecycle")


def _request(conv, **extra):  # noqa: ANN001, ANN003, ANN202
    return ChatRequest(
        conversation_id=str(conv.id),
        messages=[{"role": "user", "content": "plan my trip", **extra.pop("message", {})}],
        model="composer-model",
        agent="hermes",
        **extra,
    )


def _patches(conv, pubsub, signal, **overrides):  # noqa: ANN001, ANN003, ANN202
    patches = _image_stream_patches(conv, pubsub, signal, SimpleNamespace(turn_id=uuid.uuid4()))
    patches["capabilities"] = patch.object(chat, "require_hermes_capabilities", AsyncMock())
    patches["model"] = patch.object(chat, "organization_hermes_model", AsyncMock(return_value="hermes-model"))
    patches["capability"] = patch.object(chat.authorization_service, "has_capability", AsyncMock(return_value=True))
    patches.update(overrides)
    return patches


async def _stream(conv, user, session, request, patches):  # noqa: ANN001, ANN202
    with ExitStack() as stack:
        mocks = {name: stack.enter_context(item) for name, item in patches.items()}
        try:
            await chat.chat_stream(request=request, user=user, session=session, quota=AllowQuota([]))
        except HTTPException as exc:
            return mocks, exc
    return mocks, None


async def test_a_hermes_turn_runs_on_the_organization_model_without_the_runner_tools(monkeypatch) -> None:
    monkeypatch.setattr(chat.settings, "HERMES_ENABLED", True)
    monkeypatch.setattr(chat.settings, "WEB_ENABLED", True)
    monkeypatch.setattr(chat.settings, "USER_QUESTIONS_ENABLED", True)
    conv, user, session, pubsub = _fixture()
    signal = AsyncMock(return_value="00000000-0000-0000-0000-000000000001")

    mocks, error = await _stream(conv, user, session, _request(conv), _patches(conv, pubsub, signal))

    assert error is None
    mocks["classify"].assert_not_awaited()
    mocks["allowed"].assert_awaited_once()
    assert mocks["allowed"].call_args.args[2] == "hermes-model"
    assert mocks["create"].call_args.kwargs["model"] == "hermes-model"
    payload = signal.call_args.args[2]
    assert payload["hermes"] is True and payload["model"] == "hermes-model"
    # Hermes asks the user through its clarify tool, as the turn's questions.
    assert payload["user_questions_enabled"] is True
    assert not any(
        payload[flag]
        for flag in (
            "web_tools_enabled",
            "image_generation_enabled",
            "image_editing_enabled",
        )
    )


async def test_an_ordinary_turn_is_never_marked_hermes(monkeypatch) -> None:
    conv, user, session, pubsub = _fixture()
    signal = AsyncMock(return_value="00000000-0000-0000-0000-000000000001")
    request = ChatRequest(
        conversation_id=str(conv.id), messages=[{"role": "user", "content": "hi"}], model="composer-model"
    )

    mocks, error = await _stream(conv, user, session, request, _patches(conv, pubsub, signal))

    assert error is None
    assert "hermes" not in signal.call_args.args[2]
    assert signal.call_args.args[2]["model"] == "composer-model"
    mocks["capabilities"].assert_not_awaited()


@pytest.mark.parametrize(
    ("case", "status"),
    [("switch_off", 404), ("no_capability", 403), ("no_model", 409), ("image", 422)],
)
async def test_hermes_is_refused_before_the_turn_starts(monkeypatch, case: str, status: int) -> None:
    monkeypatch.setattr(chat.settings, "HERMES_ENABLED", case != "switch_off")
    monkeypatch.setattr(chat.settings, "IMAGES_ENABLED", True)
    conv, user, session, pubsub = _fixture()
    signal = AsyncMock()
    overrides = {}
    if case == "no_capability":
        overrides["capabilities"] = patch.object(
            chat, "require_hermes_capabilities", AsyncMock(side_effect=HermesAccessDenied("hermes_access_denied"))
        )
    if case == "no_model":
        overrides["model"] = patch.object(chat, "organization_hermes_model", AsyncMock(return_value=None))
    extra = {"message": {"imageIds": [str(uuid.uuid4())]}} if case == "image" else {}

    mocks, error = await _stream(conv, user, session, _request(conv, **extra), _patches(conv, pubsub, signal, **overrides))

    assert error is not None and error.status_code == status
    mocks["begin"].assert_not_awaited()
    signal.assert_not_awaited()


def test_an_agent_and_a_workflow_cannot_be_chosen_together() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(
            conversation_id=str(uuid.uuid4()),
            messages=[{"role": "user", "content": "hi"}],
            model="m",
            agent="hermes",
            workflowId=uuid.uuid4(),
        )


async def test_keepalives_hold_the_stream_open_without_reaching_the_browser(monkeypatch) -> None:
    import asyncio
    import json

    monkeypatch.setattr(chat.settings, "HERMES_ENABLED", True)
    monkeypatch.setattr(chat.settings, "EVENT_SUBSCRIBE_TIMEOUT", 0.2)
    conv, user, session, pubsub = _fixture()
    turn = str(uuid.uuid4())

    async def messages():  # noqa: ANN202
        for _ in range(4):
            await asyncio.sleep(0.12)
            yield {"type": "message", "data": json.dumps({"type": "keepalive", "data": {}})}
        yield {"type": "message", "data": json.dumps({"type": "done", "data": {}})}

    pubsub.listen = messages
    signal = AsyncMock(return_value=turn)
    with ExitStack() as stack:
        for item in _patches(conv, pubsub, signal).values():
            stack.enter_context(item)
        response = await chat.chat_stream(request=_request(conv), user=user, session=session, quota=AllowQuota([]))
        chunks = [chunk async for chunk in response.body_iterator]

    assert not any("keepalive" in chunk for chunk in chunks)
    assert "event: done" in chunks[-1]


@pytest.mark.parametrize(
    ("case", "agents"),
    [("ready", [{"id": "hermes"}]), ("switch_off", []), ("no_capability", []), ("no_model", []), ("model_denied", [])],
)
async def test_the_composer_offers_hermes_only_when_a_turn_would_be_accepted(monkeypatch, case, agents) -> None:
    monkeypatch.setattr(chat.settings, "HERMES_ENABLED", case != "switch_off")
    _, user, session, _ = _fixture()
    capabilities = AsyncMock(side_effect=HermesAccessDenied("hermes_access_denied") if case == "no_capability" else None)
    allowed = AsyncMock(side_effect=HTTPException(status_code=403) if case == "model_denied" else None)
    with (
        patch.object(chat, "require_hermes_capabilities", capabilities),
        patch.object(
            chat, "organization_hermes_model", AsyncMock(return_value=None if case == "no_model" else "hermes-model")
        ),
        patch.object(chat, "ensure_model_allowed", allowed),
    ):
        response = await chat.chat_agents(user=user, session=session)

    assert [agent.model_dump() for agent in response.data] == agents
