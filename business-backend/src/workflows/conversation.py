from dataclasses import dataclass
from typing import Any

from temporalio import workflow


@dataclass
class ConversationInput:
    conversation_id: str
    lifecycle_generation: int = 1
    lifecycle_operation_id: str | None = None


@workflow.defn
class ConversationWorkflow:
    """Client-side stub for type-safe signal sending from the API backend.

    The actual workflow implementation runs inside conversation-runner containers
    at conversation-runner/src/workflows/conversation.py.
    This stub is never executed — it only provides Python type hints so
    Temporal's Python SDK can validate signal names and payload types at
    the call site in business-backend/src/routers/chat.py.

    Signal definitions here must stay in sync with the runner implementation.
    """

    @workflow.run
    async def run(self, input: ConversationInput) -> str:
        return ""

    @workflow.signal(name="user_message")
    async def user_message(self, message: dict[str, Any]) -> None:
        pass

    @workflow.signal(name="cancel_generation")
    async def cancel_generation(self) -> None:
        pass

    @workflow.signal(name="cancel_workflow_run")
    async def cancel_workflow_run(self, run_id: str) -> None:
        pass

    @workflow.signal(name="cancel_turn")
    async def cancel_turn(self, turn_id: str) -> None:
        pass

    @workflow.signal(name="question_available")
    async def question_available(self, question_id: str, version: int) -> None:
        pass
