import json
import re
from pathlib import Path
from typing import Any

import pytest
from temporalio import workflow
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from src.workflows.agent import AgentWorkflow
from src.workflows.conversation import ConversationInput, ConversationWorkflow
from src.workflows.multi_agent import MultiAgentWorkflow


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "interaction_histories"


@pytest.mark.asyncio
async def test_pre_explicit_evaluator_history_replays_without_a_personal_model_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_enabled", True)
    monkeypatch.setattr("src.workflows.conversation.settings.evaluator_model", "")
    history = _load_history("pre_explicit_evaluator_conversation_v1.json", "conversation-multi-agent")
    result = await Replayer(workflows=[ConversationWorkflow]).replay_workflow(history, raise_on_replay_failure=False)
    assert result.replay_failure is None


HISTORIES = {
    "pre_questions_empty_v1.json": "conversation-replay-empty-v1",
    "pre_questions_plain_turn_v1.json": "conversation-replay-plain-v1",
    "pre_questions_ask_hallucination_v1.json": "conversation-replay-ask-hallucination-v1",
    "pre_web_conversation_no_tools_v1.json": "conversation-replay-pre-web-no-tools-v1",
    "pre_web_conversation_questions_skills_v1.json": (
        "conversation-replay-pre-web-questions-skills-v1"
    ),
}
AGENT_HISTORIES = {
    "pre_questions_agent_ask_hallucination_v1.json": "agent-replay-ask-hallucination-v1",
    "pre_web_agent_external_v1.json": "agent-replay-pre-web-external-v1",
    "pre_web_agent_internal_questions_v1.json": (
        "agent-replay-pre-web-internal-questions-v1"
    ),
}
MULTI_AGENT_HISTORIES = {
    "pre_web_multi_agent_v1.json": "multi-agent-replay-pre-web-v1",
}


def _load_history(filename: str, workflow_id: str) -> WorkflowHistory:
    raw = (FIXTURE_DIR / filename).read_text(encoding="utf-8")
    return WorkflowHistory.from_json(workflow_id, json.loads(raw))


def _all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _all_strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _all_strings(child)]
    return []


@pytest.mark.asyncio
@pytest.mark.parametrize(("filename", "workflow_id"), HISTORIES.items())
async def test_pre_question_histories_replay_with_current_workflow(
    filename: str, workflow_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "src.workflows.conversation.settings.evaluator_enabled", False
    )
    history = _load_history(filename, workflow_id)

    result = await Replayer(workflows=[ConversationWorkflow]).replay_workflow(
        history, raise_on_replay_failure=False
    )

    assert result.replay_failure is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("filename", "workflow_id"), AGENT_HISTORIES.items())
async def test_pre_question_agent_histories_replay_with_current_workflow(
    filename: str, workflow_id: str
) -> None:
    history = _load_history(filename, workflow_id)

    result = await Replayer(workflows=[AgentWorkflow]).replay_workflow(
        history, raise_on_replay_failure=False
    )

    assert result.replay_failure is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("filename", "workflow_id"), MULTI_AGENT_HISTORIES.items())
async def test_pre_web_multi_agent_histories_replay_with_current_workflow(
    filename: str, workflow_id: str
) -> None:
    history = _load_history(filename, workflow_id)

    result = await Replayer(workflows=[MultiAgentWorkflow]).replay_workflow(
        history, raise_on_replay_failure=False
    )

    assert result.replay_failure is None


@pytest.mark.parametrize(
    "filename", [*HISTORIES, *AGENT_HISTORIES, *MULTI_AGENT_HISTORIES]
)
def test_committed_histories_are_sanitized(filename: str) -> None:
    raw = json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))
    strings = _all_strings(raw)

    assert not any("Bearer " in value for value in strings)
    assert not any("session-token-secret" in value for value in strings)
    assert not any(re.search(r"eyJ[A-Za-z0-9_-]{16,}\.", value) for value in strings)


@workflow.defn(name="ConversationWorkflow")
class _IncompatibleConversationWorkflow:
    @workflow.run
    async def run(self, input: ConversationInput) -> str:
        await workflow.sleep(1)
        return input.conversation_id


@pytest.mark.asyncio
async def test_replay_negative_control_detects_nondeterminism() -> None:
    history = _load_history(
        "pre_questions_plain_turn_v1.json", "conversation-replay-plain-v1"
    )

    result = await Replayer(
        workflows=[_IncompatibleConversationWorkflow]
    ).replay_workflow(history, raise_on_replay_failure=False)

    assert result.replay_failure is not None
    assert "nondetermin" in str(result.replay_failure).lower()
