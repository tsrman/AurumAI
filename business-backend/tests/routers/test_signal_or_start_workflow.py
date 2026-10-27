import ast
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from src.routers.chat import _signal_or_start_conversation_workflow
from src.workflows.conversation import ConversationInput, ConversationWorkflow


def _signal_contract(path: Path) -> dict[str, list[str]]:
    tree = ast.parse(path.read_text())
    workflow_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ConversationWorkflow"
    )
    contract: dict[str, list[str]] = {}
    for node in workflow_class.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if ast.unparse(decorator.func) != "workflow.signal":
                continue
            name_keyword = next(
                keyword for keyword in decorator.keywords if keyword.arg == "name"
            )
            signal_name = ast.literal_eval(name_keyword.value)
            contract[signal_name] = [
                ast.unparse(argument.annotation)
                for argument in node.args.args[1:]
                if argument.annotation is not None
            ]
    return contract


def test_backend_stub_and_runner_signal_contracts_stay_identical() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    repo_root = backend_root.parent
    backend_contract = _signal_contract(
        backend_root / "src/workflows/conversation.py"
    )
    runner_contract = _signal_contract(
        repo_root / "conversation-runner/src/workflows/conversation.py"
    )

    assert backend_contract == runner_contract == {
        "user_message": ["dict[str, Any]"],
        "cancel_generation": [],
        "cancel_workflow_run": ["str"],
        "cancel_turn": ["str"],
        "question_available": ["str", "int"],
        "hermes_turn_result": ["dict[str, Any]"],
        "hermes_question": ["dict[str, Any]"],
        "hermes_question_settled": ["dict[str, Any]"],
    }


@pytest.mark.asyncio
async def test_signal_or_start_uses_atomic_signal_with_start() -> None:
    """Regression test: a plain
    signal-then-catch-NOT_FOUND-then-start used to race — two concurrent
    first messages for a brand-new conversation both got NOT_FOUND from the
    signal attempt, then both called start_workflow with the same
    workflow_id; the loser got an uncaught WorkflowAlreadyStartedError
    (500) *after* its message was already saved to the DB. This asserts
    the call now goes through Temporal's atomic signal-with-start
    (start_signal + id_conflict_policy=USE_EXISTING), which the server
    handles as a single RPC with no client-side race window."""
    client = AsyncMock(spec=Client)

    message = {"role": "user", "content": "hi", "turn_id": "t1", "model": "gpt-4o-mini"}
    await _signal_or_start_conversation_workflow(client, "conv-1", message)

    client.start_workflow.assert_awaited_once()
    _, kwargs = client.start_workflow.call_args
    assert kwargs["id"] == "conversation-conv-1"
    assert kwargs["task_queue"] == "session-conv-1"
    assert kwargs["id_reuse_policy"] == WorkflowIDReusePolicy.ALLOW_DUPLICATE
    assert kwargs["id_conflict_policy"] == WorkflowIDConflictPolicy.USE_EXISTING
    assert kwargs["start_signal"] == "user_message"
    assert kwargs["start_signal_args"] == [message]

    args, _ = client.start_workflow.call_args
    assert args[0] == ConversationWorkflow.run
    assert args[1] == ConversationInput(conversation_id="conv-1")

    # No separate signal call — start_signal handles delivery atomically,
    # whether the workflow is new or already running.
    client.get_workflow_handle.assert_not_called()
