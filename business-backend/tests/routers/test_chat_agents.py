"""Choosing an agent of the administrator's in chat: the agent's model, prompt and
tools scope the turn, and nothing the user may not use starts one."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.routers import chat
from src.schemas.chat import ChatRequest
from src.services.agent_authorization import (
    AgentDefinitionNotFound,
    AgentDependencyAccessDenied,
    ChatAgentTurn,
)
from tests.routers.test_chat_hermes import _stream
from tests.routers.test_chat_workflow_runtime import _fixture, _image_stream_patches

pytestmark = pytest.mark.usefixtures("mock_chat_lifecycle")

AGENT_ID = uuid.uuid4()
SKILL_ID = uuid.uuid4()
BASE_ID = uuid.uuid4()


def _agent(**overrides):  # noqa: ANN003, ANN202
    values = {
        "id": AGENT_ID,
        "name": "Procedures assistant",
        "model_id": "agent-model",
        "system_prompt": "Answer from the procedures only.",
        "temperature": 0.2,
        "allowed_skill_ids": [str(SKILL_ID)],
        "knowledge_bases": [{"id": str(BASE_ID), "name": "Procedures"}],
        "web_enabled": True,
        "images_enabled": True,
        "ask_user_enabled": True,
        "kind": "agent",
    }
    values.update(overrides)
    return ChatAgentTurn(**values)


def _request(conv, **extra):  # noqa: ANN001, ANN003, ANN202
    return ChatRequest(
        conversation_id=str(conv.id),
        messages=[{"role": "user", "content": "what is the allowed pH"}],
        model="composer-model",
        agentDefinitionId=AGENT_ID,
        **extra,
    )


def _patches(conv, pubsub, signal, resolver=None, **overrides):  # noqa: ANN001, ANN003, ANN202
    patches = _image_stream_patches(conv, pubsub, signal, SimpleNamespace(turn_id=uuid.uuid4()))
    patches["resolve"] = patch.object(
        chat.agent_authorization_service, "require_chat_agent", resolver or AsyncMock(return_value=_agent())
    )
    patches["capability"] = patch.object(chat.authorization_service, "has_capability", AsyncMock(return_value=True))
    patches.update(overrides)
    return patches


def _platform(monkeypatch, *, web=True, images=True, questions=True):  # noqa: ANN001, ANN202
    monkeypatch.setattr(chat.settings, "WEB_ENABLED", web)
    monkeypatch.setattr(chat.settings, "IMAGES_ENABLED", images)
    monkeypatch.setattr(chat.settings, "USER_QUESTIONS_ENABLED", questions)


def _image_models():  # noqa: ANN202
    return {
        "image_models": patch.object(chat, "organization_image_models", AsyncMock(return_value=("gen-model", "edit-model"))),
        "recent_images": patch.object(chat, "recent_conversation_images", AsyncMock(return_value=[])),
    }


FLAGS = ("web_tools_enabled", "image_generation_enabled", "image_editing_enabled", "user_questions_enabled")


async def test_an_agent_turn_runs_on_the_agents_model_with_its_prompt_and_tool_scope(monkeypatch) -> None:
    _platform(monkeypatch)
    conv, user, session, pubsub = _fixture()
    conv.temperature = None
    conv.agent_definition_id = None
    signal = AsyncMock(return_value="00000000-0000-0000-0000-000000000001")

    mocks, error = await _stream(
        conv, user, session, _request(conv), _patches(conv, pubsub, signal, **_image_models())
    )

    assert error is None
    mocks["classify"].assert_not_awaited()
    # Quota and the allow-list see the agent's model, not the composer's.
    assert mocks["allowed"].call_args.args[2] == "agent-model"
    assert mocks["create"].call_args.kwargs["model"] == "agent-model"
    payload = signal.call_args.args[2]
    assert payload["model"] == "agent-model"
    assert payload["agent"] == {
        "id": str(AGENT_ID),
        "name": "Procedures assistant",
        "system_prompt": "Answer from the procedures only.",
        "allowed_skill_ids": [str(SKILL_ID)],
        "knowledge_bases": [{"id": str(BASE_ID), "name": "Procedures"}],
    }
    assert payload["temperature"] == 0.2
    # All three built-in capabilities are on for this agent and allowed for the user.
    assert all(payload[flag] for flag in ("web_tools_enabled", "image_generation_enabled", "image_editing_enabled"))
    assert payload["user_questions_enabled"] is True
    assert conv.agent_definition_id == AGENT_ID


async def test_the_conversations_own_temperature_beats_the_agents(monkeypatch) -> None:
    conv, user, session, pubsub = _fixture()
    conv.temperature = 1.1
    conv.agent_definition_id = None
    signal = AsyncMock(return_value="00000000-0000-0000-0000-000000000001")

    await _stream(conv, user, session, _request(conv), _patches(conv, pubsub, signal))

    assert signal.call_args.args[2]["temperature"] == 1.1


async def test_a_plain_turn_clears_the_conversations_agent() -> None:
    conv, user, session, pubsub = _fixture()
    conv.temperature = None
    conv.agent_definition_id = AGENT_ID
    signal = AsyncMock(return_value="00000000-0000-0000-0000-000000000001")
    request = ChatRequest(conversation_id=str(conv.id), messages=[{"role": "user", "content": "hi"}], model="m")

    mocks, error = await _stream(conv, user, session, request, _patches(conv, pubsub, signal))

    assert error is None
    assert "agent" not in signal.call_args.args[2]
    assert conv.agent_definition_id is None
    mocks["resolve"].assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (AgentDefinitionNotFound(AGENT_ID), 404),  # missing, archived or not granted
        (AgentDependencyAccessDenied(AGENT_ID), 403),
    ],
)
async def test_an_agent_the_user_may_not_use_is_refused_before_the_turn_starts(error, status) -> None:  # noqa: ANN001
    conv, user, session, pubsub = _fixture()
    conv.temperature = None
    conv.agent_definition_id = None
    signal = AsyncMock()

    mocks, raised = await _stream(
        conv, user, session, _request(conv), _patches(conv, pubsub, signal, resolver=AsyncMock(side_effect=error))
    )

    assert isinstance(raised, HTTPException) and raised.status_code == status
    mocks["begin"].assert_not_awaited()
    signal.assert_not_awaited()


def test_at_most_one_of_an_agent_a_system_agent_and_a_workflow() -> None:
    base = {"conversation_id": str(uuid.uuid4()), "messages": [{"role": "user", "content": "hi"}], "model": "m"}
    for extra in (
        {"agent": "hermes", "agentDefinitionId": uuid.uuid4()},
        {"workflowId": uuid.uuid4(), "agentDefinitionId": uuid.uuid4()},
    ):
        with pytest.raises(ValidationError):
            ChatRequest(**base, **extra)


@pytest.mark.parametrize(
    ("switch", "flags_off"),
    [
        ("web_enabled", {"web_tools_enabled"}),
        ("images_enabled", {"image_generation_enabled", "image_editing_enabled"}),
        ("ask_user_enabled", {"user_questions_enabled"}),
    ],
)
async def test_each_capability_of_an_agent_is_switched_on_its_own(monkeypatch, switch, flags_off) -> None:  # noqa: ANN001
    _platform(monkeypatch)
    conv, user, session, pubsub = _fixture()
    signal = AsyncMock(return_value="00000000-0000-0000-0000-000000000001")
    resolver = AsyncMock(return_value=_agent(**{switch: False}))

    await _stream(conv, user, session, _request(conv), _patches(conv, pubsub, signal, resolver=resolver, **_image_models()))

    payload = signal.call_args.args[2]
    assert {flag for flag in FLAGS if not payload[flag]} == flags_off


async def test_an_agent_never_widens_what_the_platform_or_the_user_allows(monkeypatch) -> None:
    _platform(monkeypatch, web=False, images=False, questions=False)
    conv, user, session, pubsub = _fixture()
    signal = AsyncMock(return_value="00000000-0000-0000-0000-000000000001")

    await _stream(conv, user, session, _request(conv), _patches(conv, pubsub, signal, **_image_models()))

    assert not any(signal.call_args.args[2][flag] for flag in FLAGS)

    # The platform allows everything, the user lacks the capabilities.
    _platform(monkeypatch)
    signal.reset_mock()
    no_rights = patch.object(chat.authorization_service, "has_capability", AsyncMock(return_value=False))
    await _stream(
        conv, user, session, _request(conv), _patches(conv, pubsub, signal, capability=no_rights, **_image_models())
    )

    payload = signal.call_args.args[2]
    assert not any(payload[flag] for flag in ("web_tools_enabled", "image_generation_enabled", "image_editing_enabled"))


def _hermes_patches(conv, pubsub, signal, **overrides):  # noqa: ANN001, ANN003, ANN202
    extra = {
        "hermes_capabilities": patch.object(chat, "require_hermes_capabilities", AsyncMock()),
        "hermes_model": patch.object(chat, "organization_hermes_model", AsyncMock(return_value="hermes-model")),

        **overrides,
    }
    resolver = AsyncMock(
        return_value=_agent(
            kind="hermes",
            name="Finance analyst",
            system_prompt="You are the finance analyst.",
            allowed_skill_ids=[],
            knowledge_bases=[],
        )
    )
    return _patches(conv, pubsub, signal, resolver=resolver, **extra)


async def test_a_hermes_agent_turn_runs_hermes_on_the_organizations_model_with_the_agents_instructions(monkeypatch) -> None:
    monkeypatch.setattr(chat.settings, "HERMES_ENABLED", True)
    monkeypatch.setattr(chat.settings, "USER_QUESTIONS_ENABLED", True)
    conv, user, session, pubsub = _fixture()
    signal = AsyncMock(return_value="00000000-0000-0000-0000-000000000001")

    mocks, error = await _stream(conv, user, session, _request(conv), _hermes_patches(conv, pubsub, signal))

    assert error is None
    mocks["classify"].assert_not_awaited()
    # The organization's current Hermes model, not the one stored on the agent.
    assert mocks["allowed"].call_args.args[2] == "hermes-model"
    payload = signal.call_args.args[2]
    assert payload["hermes"] is True and payload["model"] == "hermes-model"
    assert payload["agent"]["system_prompt"] == "You are the finance analyst."
    assert payload["user_questions_enabled"] is True
    assert not any(payload[flag] for flag in ("web_tools_enabled", "image_generation_enabled", "image_editing_enabled"))
    assert conv.agent_definition_id == AGENT_ID


@pytest.mark.parametrize(
    ("case", "status"),
    [("switch_off", 404), ("no_capability", 403), ("no_model", 409)],
)
async def test_a_hermes_agent_needs_hermes_to_be_on_allowed_and_set_up(monkeypatch, case, status) -> None:  # noqa: ANN001
    from src.services.hermes_access import HermesAccessDenied

    monkeypatch.setattr(chat.settings, "HERMES_ENABLED", case != "switch_off")
    conv, user, session, pubsub = _fixture()
    signal = AsyncMock()
    overrides = {}
    if case == "no_capability":
        overrides["hermes_capabilities"] = patch.object(
            chat, "require_hermes_capabilities", AsyncMock(side_effect=HermesAccessDenied("hermes_access_denied"))
        )
    if case == "no_model":
        overrides["hermes_model"] = patch.object(chat, "organization_hermes_model", AsyncMock(return_value=None))

    mocks, error = await _stream(conv, user, session, _request(conv), _hermes_patches(conv, pubsub, signal, **overrides))

    assert error is not None and error.status_code == status
    mocks["begin"].assert_not_awaited()


async def test_a_hermes_agent_does_not_take_images(monkeypatch) -> None:
    monkeypatch.setattr(chat.settings, "HERMES_ENABLED", True)
    conv, user, session, pubsub = _fixture()
    request = ChatRequest(
        conversation_id=str(conv.id),
        messages=[{"role": "user", "content": "look", "imageIds": [str(uuid.uuid4())]}],
        model="composer-model",
        agentDefinitionId=AGENT_ID,
    )

    mocks, error = await _stream(conv, user, session, request, _hermes_patches(conv, pubsub, AsyncMock()))

    assert error is not None and error.status_code == 422
    mocks["begin"].assert_not_awaited()


async def test_a_conversation_with_a_hermes_agents_workspace_keeps_that_agent(monkeypatch) -> None:
    monkeypatch.setattr(chat.settings, "HERMES_ENABLED", True)
    conv, user, session, pubsub = _fixture()
    conv.hermes_agent_definition_id = uuid.uuid4()  # another agent holds this conversation's workspace
    signal = AsyncMock()

    mocks, error = await _stream(conv, user, session, _request(conv), _hermes_patches(conv, pubsub, signal))

    assert error is not None and error.status_code == 409
    mocks["begin"].assert_not_awaited()
    signal.assert_not_awaited()


async def test_the_first_hermes_agent_turn_claims_the_workspace_and_later_plain_turns_keep_the_claim(monkeypatch) -> None:
    monkeypatch.setattr(chat.settings, "HERMES_ENABLED", True)
    conv, user, session, pubsub = _fixture()
    signal = AsyncMock(return_value="00000000-0000-0000-0000-000000000001")

    await _stream(conv, user, session, _request(conv), _hermes_patches(conv, pubsub, signal))
    assert conv.hermes_agent_definition_id == AGENT_ID

    plain = ChatRequest(conversation_id=str(conv.id), messages=[{"role": "user", "content": "hi"}], model="m")
    await _stream(conv, user, session, plain, _patches(conv, pubsub, signal))

    assert conv.agent_definition_id is None  # the picker is back on the plain chat
    assert conv.hermes_agent_definition_id == AGENT_ID  # but the workspace still belongs to the agent


def test_a_bound_agent_takes_precedence_over_the_generic_hermes_in_the_conversation_view() -> None:
    from datetime import datetime, timezone

    conv = SimpleNamespace(
        id=uuid.uuid4(), title="t", model="m", status="active", last_message_at=None,
        created_at=datetime.now(timezone.utc), temperature=None, agent_definition_id=AGENT_ID,
    )

    bound = chat._conv_response(conv, None, hermes=True)
    plain = chat._conv_response(SimpleNamespace(**{**conv.__dict__, "agent_definition_id": None}), None, hermes=True)

    assert (bound.agent, bound.agentDefinitionId) == (None, str(AGENT_ID))
    assert (plain.agent, plain.agentDefinitionId) == ("hermes", None)
