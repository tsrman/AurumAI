from typing import Any

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from src import hermes_activities, hermes_questions
from src.hermes_questions import question
from tests.test_hermes_activities import SESSION, http  # noqa: F401

REQUEST = {
    "event": "clarify.request",
    "prompt_id": "b0256703ac",
    "execution_generation": 1,
    "question": 'Which city?\nReply "skip" to skip this question.',
    "choices": ["Paris (Recommended)", "Rome"],
    "multi_select": False,
}


def test_a_question_is_bounded_and_loses_hermess_own_hints() -> None:
    assert question(REQUEST, "run_1", "turn-1") == {
        "turn_id": "turn-1",
        "run_id": "run_1",
        "prompt_id": "b0256703ac",
        "execution_generation": 1,
        "text": "Which city?",
        "choices": ["Paris", "Rome"],
        "multi_select": False,
    }
    long = question({**REQUEST, "question": "q" * 9000, "choices": [f"c{i}" * 300 for i in range(20)]}, "r", "t")
    assert long is not None and len(long["text"]) == 4000
    assert len(long["choices"]) == 8 and all(len(choice) <= 200 for choice in long["choices"])


@pytest.mark.parametrize(
    "change",
    [{"prompt_id": "../../x"}, {"prompt_id": 5}, {"execution_generation": "1"}, {"question": None}, {"question": "  "}],
)
def test_a_malformed_question_is_dropped(change: dict[str, Any]) -> None:
    assert question({**REQUEST, **change}, "run_1", "turn-1") is None


@pytest.mark.asyncio
async def test_the_run_forwards_its_questions_and_their_settling(http, monkeypatch) -> None:  # noqa: F811
    import json

    signals: list[tuple[str, str, dict[str, Any]]] = []

    class Handle:
        def __init__(self, workflow_id: str) -> None:
            self.workflow_id = workflow_id

        async def signal(self, name: str, payload: dict[str, Any]) -> None:
            signals.append((self.workflow_id, name, payload))

    class Client:
        def get_workflow_handle(self, workflow_id: str) -> Handle:
            return Handle(workflow_id)

    monkeypatch.setattr(hermes_questions.activity, "client", lambda: Client())
    lines = [
        {"type": "started", "run_id": "run_1"},
        {"type": "event", "event": REQUEST},
        {"type": "event", "event": {"event": "clarify.settled", "prompt_id": "b0256703ac", "execution_generation": 1}},
        {"type": "result", "run_id": "run_1", "status": "completed", "output": "done"},
    ]
    http(
        {
            ("POST", f"/hermes/sandboxes/{SESSION}/runs"): httpx.Response(
                200, content="".join(json.dumps(line) + "\n" for line in lines).encode()
            )
        }
    )

    await ActivityEnvironment().run(
        hermes_activities.hermes_run_turn, SESSION, "turn-1", "conv", "hi", [], "", "conversation-conv"
    )

    assert [(workflow_id, name) for workflow_id, name, _ in signals] == [
        ("conversation-conv", "hermes_question"),
        ("conversation-conv", "hermes_question_settled"),
    ]
    assert signals[0][2]["run_id"] == "run_1" and signals[1][2] == {"turn_id": "turn-1", "prompt_id": "b0256703ac"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (httpx.Response(200, json={"status": "answered"}), "answered"),
        (httpx.Response(409, json={"detail": {"code": "hermes_clarify_settled"}}), "settled"),
    ],
)
async def test_an_answer_reaches_the_run(http, response: httpx.Response, outcome: str) -> None:  # noqa: F811
    router = http({("POST", f"/hermes/sandboxes/{SESSION}/runs/run_1/clarify"): response})

    result = await ActivityEnvironment().run(
        hermes_activities.hermes_answer_question, SESSION, "run_1", "p1", 1, "Rome"
    )

    assert result == outcome
    assert router.requests[0][2] == {"request_id": "p1", "execution_generation": 1, "answer": "Rome"}
