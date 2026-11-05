"""What a Hermes agent definition changes for its sessions: the bridge's tools, skills,
knowledge bases, the internet mode and the model. A session with no agent keeps the
organization's settings."""

import uuid
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport
from pydantic import SecretStr
from sqlalchemy import select, update

from src.core.capability_catalog import Capability
from src.core.config import settings
from src.models.agent_definition import AgentDefinition
from src.models.conversation import Conversation
from src.models.interaction import InteractionExecution
from src.services import hermes_mcp
from src.services.hermes_egress import set_organization_egress_mode
from tests.hermes.conftest import scripted_stream
from tests.hermes.test_hermes_egress import SECRET, _authorize
from tests.hermes.test_hermes_mcp import RecordingService, _call, _request, _rpc, _tool_payload
from tests.hermes.test_hermes_mcp_skills import _skill


@pytest.fixture(autouse=True)
def _switches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WEB_ENABLED", True)
    monkeypatch.setattr(settings, "IMAGES_ENABLED", True)
    monkeypatch.setattr(settings, "HERMES_EGRESS_SERVICE_SECRET", SecretStr(SECRET))


async def _agent(env, *, chat: bool = True, **values: Any) -> uuid.UUID:  # noqa: ANN001
    """A Hermes agent, bound to the conversation (chat) or to the bound execution (workflow node)."""
    agent_id = uuid.uuid4()
    values.setdefault("model_id", "agent-model")
    async with env.sessions() as db:
        db.add(
            AgentDefinition(
                id=agent_id,
                org_id=env.org_id,
                name=f"agent-{agent_id.hex[:6]}",
                system_prompt="Be useful.",
                kind="hermes",
                **values,
            )
        )
        await db.flush()
        if chat:
            conversation = await db.scalar(select(Conversation).where(Conversation.id == env.conversation_id))
            conversation.hermes_agent_definition_id = agent_id
        else:
            await db.execute(
                update(InteractionExecution)
                .where(InteractionExecution.id == env.execution_id)
                .values(agent_definition_id=agent_id)
            )
        await db.commit()
    return agent_id


async def _names(env, token: str) -> list[str]:  # noqa: ANN001
    listed = await _rpc(env, token, _request("tools/list"))
    return [tool["name"] for tool in listed.json()["result"]["tools"]]


async def test_a_session_without_an_agent_keeps_every_tool(hermes_env) -> None:
    opened = await hermes_env.open_session()

    names = await _names(hermes_env, opened["token"])

    assert {"web_search", "generate_image", "run_skill"} <= set(names)


@pytest.mark.parametrize("chat", [True, False])
async def test_switched_off_web_and_images_are_neither_listed_nor_callable(
    hermes_env, monkeypatch, chat: bool
) -> None:
    service = RecordingService()
    monkeypatch.setattr(hermes_mcp, "web_tools_service", service)
    monkeypatch.setattr(hermes_mcp, "image_tools_service", service)
    await _agent(hermes_env, chat=chat, web_enabled=False, images_enabled=False)
    opened = await hermes_env.open_session()

    names = await _names(hermes_env, opened["token"])
    web = _tool_payload(await _rpc(hermes_env, opened["token"], _call("web_search", {"query": "x"})))
    image = _tool_payload(await _rpc(hermes_env, opened["token"], _call("generate_image", {"prompt": "x"})))

    assert not {"web_search", "web_read", "generate_image", "edit_image"} & set(names)
    assert web == (True, {"error": "web_disabled"})
    assert image == (True, {"error": "image_disabled"})
    assert service.calls == []
    async with hermes_env.sessions() as db:
        # The execution outlives the test's organization otherwise.
        await db.execute(
            update(InteractionExecution)
            .where(InteractionExecution.id == hermes_env.execution_id)
            .values(agent_definition_id=None)
        )
        await db.commit()


async def test_an_archived_agent_stops_its_sessions(hermes_env) -> None:
    from datetime import UTC, datetime

    await _agent(hermes_env, archived_at=datetime.now(UTC))
    opened = await hermes_env.open_session()

    result = _tool_payload(await _rpc(hermes_env, opened["token"], _call("list_skills", {})))
    egress = (await _authorize(hermes_env, opened["token"])).json()

    assert result == (True, {"error": "hermes_agent_unavailable"})
    assert (egress["allow"], egress["reason"]) == (False, "hermes_agent_unavailable")


async def test_selected_skills_limit_listing_and_running(hermes_env, monkeypatch) -> None:
    mine = await _skill(hermes_env, name="mine")
    other = await _skill(hermes_env, name="other")
    await _agent(hermes_env, hermes_skills_scope="selected", skill_ids=[mine])
    opened = await hermes_env.open_session()

    listed = _tool_payload(await _rpc(hermes_env, opened["token"], _call("list_skills", {})))
    run = _tool_payload(await _rpc(hermes_env, opened["token"], _call("run_skill", {"skill_id": str(other)})))

    assert [skill["skillId"] for skill in listed[1]["skills"]] == [str(mine)]
    assert run == (True, {"error": "skill_not_found"})


async def test_all_scope_keeps_every_skill_of_the_user(hermes_env) -> None:
    mine = await _skill(hermes_env, name="mine")
    other = await _skill(hermes_env, name="other")
    await _agent(hermes_env, skill_ids=[mine])
    opened = await hermes_env.open_session()

    listed = _tool_payload(await _rpc(hermes_env, opened["token"], _call("list_skills", {})))

    assert {skill["skillId"] for skill in listed[1]["skills"]} == {str(mine), str(other)}


async def test_knowledge_search_reaches_only_the_agents_bases(hermes_env, monkeypatch) -> None:
    allowed, foreign = uuid.uuid4(), uuid.uuid4()
    await _agent(hermes_env, knowledge_base_ids=[allowed])
    opened = await hermes_env.open_session()

    refused = _tool_payload(
        await _rpc(
            hermes_env,
            opened["token"],
            _call("knowledge_search", {"query": "x", "knowledgeBaseId": str(foreign)}),
        )
    )

    assert refused == (True, {"error": "knowledge_not_found"})


async def test_knowledge_search_by_id_needs_an_agent(hermes_env) -> None:
    opened = await hermes_env.open_session()

    refused = _tool_payload(
        await _rpc(
            hermes_env,
            opened["token"],
            _call("knowledge_search", {"query": "x", "knowledgeBaseId": str(uuid.uuid4())}),
        )
    )

    assert refused == (True, {"error": "knowledge_not_found"})


@pytest.mark.parametrize(
    ("organization", "agent", "expected"),
    [
        ("public", "none", "none"),
        ("public", "packages", "packages"),
        ("packages", "public", "packages"),
        ("packages", None, "packages"),
    ],
)
async def test_the_agent_only_lowers_the_internet_mode(
    hermes_env, monkeypatch, organization: str, agent: str | None, expected: str
) -> None:
    monkeypatch.setattr(settings, "HERMES_EGRESS_MAX_MODE", "public")
    await hermes_env.grant(Capability.WEB_ACCESS)
    async with hermes_env.sessions() as session:
        session.info["org_id"] = str(hermes_env.org_id)
        await set_organization_egress_mode(session, hermes_env.org_id, organization)  # type: ignore[arg-type]
        await session.commit()
    await _agent(hermes_env, hermes_egress_mode=agent)
    opened = await hermes_env.open_session()

    body = (await _authorize(hermes_env, opened["token"])).json()

    assert (body["allow"], body["mode"]) == (True, expected)


async def test_the_agents_own_model_replaces_the_requested_one(hermes_env) -> None:
    await _agent(hermes_env, use_org_hermes_model=False, model_id="agent-model")
    opened = await hermes_env.open_session()
    requests: list[dict[str, Any]] = []
    body = {"model": "requested-model", "messages": [{"role": "user", "content": "hi"}], "stream": True}

    with patch(
        "src.services.completion_stream.llm_client.stream_chat",
        side_effect=scripted_stream(["ok"], requests),
    ):
        async with httpx.AsyncClient(transport=ASGITransport(app=hermes_env.app), base_url="http://test") as client:
            response = await client.post(
                "/v1/hermes/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {opened['token']}"},
            )

    assert response.status_code == 200, response.text
    assert requests[0]["model"] == "agent-model"
