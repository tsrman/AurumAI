import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from src.hermes_session_workflow import HermesSessionWorkflow

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_a_session_recorded_before_questions_replays() -> None:
    raw = json.loads((FIXTURES / "pre_clarify_hermes_session_v1.json").read_text(encoding="utf-8"))
    history = WorkflowHistory.from_json("hermes-session-conv-1", raw)

    result = await Replayer(workflows=[HermesSessionWorkflow]).replay_workflow(history, raise_on_replay_failure=False)

    assert result.replay_failure is None
