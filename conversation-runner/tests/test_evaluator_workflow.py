import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.workflows.evaluator import EvaluationInput, EvaluationWorkflow


def _make_llm_call(response: str):
    calls: list[dict] = []
    publish_flags: list[bool] = []

    @activity.defn(name="llm_call")
    async def llm_call(
        request: dict, turn_id: str | None = None, publish_events: bool = True
    ) -> str:
        calls.append(request)
        publish_flags.append(publish_events)
        return response

    return llm_call, calls, publish_flags


def _make_record_langfuse_score():
    calls: list[tuple] = []

    @activity.defn(name="record_langfuse_score")
    async def record_langfuse_score(
        trace_id: str, name: str, value: float, comment: str = ""
    ) -> None:
        calls.append((trace_id, name, value, comment))

    return record_langfuse_score, calls


def _make_record_message_score():
    calls: list[tuple] = []

    @activity.defn(name="record_message_score")
    async def record_message_score(
        turn_id: str, score: float | None, reason: str | None
    ) -> None:
        calls.append((turn_id, score, reason))

    return record_message_score, calls


async def _run_evaluation_workflow(input: EvaluationInput, activities: list):
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-tq",
            workflows=[EvaluationWorkflow],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                EvaluationWorkflow.run,
                input,
                id="eval-test",
                task_queue="test-tq",
            )
            return await handle.result()


@pytest.mark.asyncio
async def test_evaluation_records_score_from_valid_json():
    llm_call, llm_calls, publish_flags = _make_llm_call(
        '{"score": 1, "reason": "fully supported"}'
    )
    record_score, score_calls = _make_record_langfuse_score()
    record_msg_score, msg_score_calls = _make_record_message_score()

    await _run_evaluation_workflow(
        EvaluationInput(
            conversation_id="conv-1",
            turn_id="turn-1",
            query="What is the capital?",
            context_used="Paris is the capital of France.",
            agent_answer="Paris.",
            model="test-model",
        ),
        [llm_call, record_score, record_msg_score],
    )

    assert len(llm_calls) == 1
    assert score_calls == [("conv-1", "faithfulness", 1.0, "fully supported")]
    assert msg_score_calls == [("turn-1", 1.0, "fully supported")]
    # The judge call must never surface on the user-facing SSE channel.
    assert publish_flags == [False]


@pytest.mark.asyncio
async def test_evaluation_parses_markdown_fenced_json():
    llm_call, _, _ = _make_llm_call('```json\n{"score": 0, "reason": "hallucinated"}\n```')
    record_score, score_calls = _make_record_langfuse_score()
    record_msg_score, _ = _make_record_message_score()

    await _run_evaluation_workflow(
        EvaluationInput(
            conversation_id="conv-2",
            turn_id="turn-2",
            query="q",
            context_used="ctx",
            agent_answer="answer",
            model="test-model",
        ),
        [llm_call, record_score, record_msg_score],
    )

    assert score_calls == [("conv-2", "faithfulness", 0.0, "hallucinated")]


@pytest.mark.asyncio
async def test_evaluation_swallows_malformed_json_and_skips_scoring():
    llm_call, _, _ = _make_llm_call("not valid json at all")
    record_score, score_calls = _make_record_langfuse_score()
    record_msg_score, msg_score_calls = _make_record_message_score()

    result = await _run_evaluation_workflow(
        EvaluationInput(
            conversation_id="conv-3",
            turn_id="turn-3",
            query="q",
            context_used="ctx",
            agent_answer="answer",
            model="test-model",
        ),
        [llm_call, record_score, record_msg_score],
    )

    assert result is None
    assert score_calls == []
    # Backend is still notified (score=None) so a waiting SSE connection
    # doesn't idle out on every evaluator failure.
    assert len(msg_score_calls) == 1
    assert msg_score_calls[0][0] == "turn-3"
    assert msg_score_calls[0][1] is None


@pytest.mark.asyncio
async def test_evaluation_defaults_missing_score_and_reason():
    llm_call, _, _ = _make_llm_call("{}")
    record_score, score_calls = _make_record_langfuse_score()
    record_msg_score, _ = _make_record_message_score()

    await _run_evaluation_workflow(
        EvaluationInput(
            conversation_id="conv-4",
            turn_id="turn-4",
            query="q",
            context_used="ctx",
            agent_answer="answer",
            model="test-model",
        ),
        [llm_call, record_score, record_msg_score],
    )

    assert score_calls == [("conv-4", "faithfulness", 0.0, "")]


@pytest.mark.asyncio
async def test_internal_context_keeps_the_score_but_hides_the_reason():
    """A multi-agent turn judges the answer against the raw node outputs, which
    are admin-only.  record_message_score's reason is rendered to the user, and
    the judge quotes its context freely — so the score is kept and the
    justification dropped.  Langfuse, an operator surface, still gets it."""
    llm_call, _, _ = _make_llm_call(
        '{"score": 0, "reason": "node beta reported internal revenue of 4.2M"}'
    )
    record_score, score_calls = _make_record_langfuse_score()
    record_msg_score, msg_score_calls = _make_record_message_score()

    await _run_evaluation_workflow(
        EvaluationInput(
            conversation_id="conv-1",
            turn_id="turn-1",
            query="Summarize",
            context_used="node beta: internal revenue 4.2M",
            agent_answer="Revenue grew.",
            model="test-model",
            reveal_reason=False,
        ),
        [llm_call, record_score, record_msg_score],
    )

    assert msg_score_calls == [("turn-1", 0.0, None)]
    assert score_calls == [
        ("conv-1", "faithfulness", 0.0, "node beta reported internal revenue of 4.2M")
    ]
