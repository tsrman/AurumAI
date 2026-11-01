"""A chat turn on an agent of the administrator's: its instructions lead the
prompt, only its skills are offered, its knowledge bases are named, and its
temperature (when the signal carries one) is the request's."""

from temporalio import activity

from tests.test_conversation_tool_loop import (
    _make_llm_call,
    _make_persist_assistant_message,
    _make_publish_event,
    _run_one_turn,
    mock_get_conversation_temperature,
    mock_get_session_token,
    mock_load_conversation_state,
)

AGENT = {
    "id": "agent-1",
    "name": "Procedures assistant",
    "system_prompt": "Answer from the procedures only.",
    "allowed_skill_ids": ["allowed-skill"],
    "knowledge_bases": [{"id": "base-1", "name": "Procedures"}],
}


@activity.defn(name="list_available_skills")
async def _two_skills() -> list[dict]:
    return [
        {"id": "allowed-skill", "name": "Search", "description": "search docs", "type": "tool", "inputSchema": {}},
        {"id": "other-skill", "name": "Weather", "description": "forecast", "type": "tool", "inputSchema": {}},
    ]


async def _turn(message: dict) -> dict:
    llm_call, llm_calls = _make_llm_call("Done.")
    publish_event, _ = _make_publish_event()
    persist, _ = _make_persist_assistant_message()
    await _run_one_turn(
        [
            mock_load_conversation_state,
            _two_skills,
            mock_get_session_token,
            mock_get_conversation_temperature,
            llm_call,
            publish_event,
            persist,
        ],
        [],
        {"role": "user", "content": "what is the allowed pH", "turn_id": "t1", "model": "agent-model", **message},
    )
    return llm_calls[0]


async def test_an_agent_turn_is_scoped_to_the_agents_prompt_skills_and_knowledge_bases() -> None:
    request = await _turn({"agent": AGENT, "temperature": 0.2})

    system = request["messages"][0]["content"]
    assert system.startswith("Answer from the procedures only.")
    assert "allowed-skill" in system and "other-skill" not in system
    assert "id=base-1 name=Procedures" in system
    assert request["model"] == "agent-model"
    assert request["temperature"] == 0.2


async def test_a_plain_turn_still_offers_every_skill_and_the_conversations_temperature() -> None:
    request = await _turn({})

    system = request["messages"][0]["content"]
    assert "allowed-skill" in system and "other-skill" in system
    assert request["temperature"] == 0.7


async def test_an_agent_without_its_own_temperature_uses_the_resolved_one() -> None:
    request = await _turn({"agent": AGENT})

    assert request["temperature"] == 0.7
