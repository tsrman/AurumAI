"""Real Temporal parent/child histories, including terminal parent/live child."""

import uuid
import os
from datetime import timedelta

import pytest
from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.testing import WorkflowEnvironment


@pytest.fixture
async def receipt_client():
    endpoint = os.environ.get("ARCHIVE_TEST_TEMPORAL_HOST")
    if endpoint:
        yield await Client.connect(endpoint)
    else:
        # The local dev server supports real visibility/history APIs, including
        # multiple closed RunIDs; no shared Temporal service is needed in CI.
        async with await WorkflowEnvironment.start_local() as environment:
            yield environment.client


@workflow.defn(name="EvaluationWorkflow")
class ReceiptChild:
    @workflow.run
    async def run(self, data: dict) -> None:
        await workflow.sleep(timedelta(seconds=data["seconds"]))


@workflow.defn(name="ConversationWorkflow")
class ReceiptRoot:
    @workflow.run
    async def run(self, data: dict) -> None:
        await workflow.start_child_workflow(
            "EvaluationWorkflow",
            data,
            id=f"{data['conversation_id']}-eval-{data['turn_id']}",
            parent_close_policy=workflow.ParentClosePolicy.ABANDON,
        )


@pytest.mark.asyncio
async def test_closed_parent_does_not_prove_child_closure(receipt_client):
    from src.services.execution_closure import ExecutionClosureVerifier

    client = receipt_client
    conv, turn = uuid.uuid4(), uuid.uuid4()
    queue = f"session-{conv}"
    async with Worker(client, task_queue=queue, workflows=[ReceiptRoot, ReceiptChild]):
        handle = await client.start_workflow(
            ReceiptRoot.run,
            {"conversation_id": str(conv), "turn_id": str(turn), "seconds": 2},
            id=f"conversation-{conv}",
            task_queue=queue,
        )
        await handle.result()
        verifier = ExecutionClosureVerifier(client)
        tree = await verifier.inspect_tree(conv, handle.id, str(handle.first_execution_run_id))
        assert len(tree) == 2
        assert not all(row.closed for row in tree)
        child = next(row for row in tree if row.parent_run_id)
        await client.get_workflow_handle(child.workflow_id, run_id=str(child.run_id)).result()
        tree = await verifier.inspect_tree(conv, handle.id, str(handle.first_execution_run_id))
        assert all(row.closed for row in tree)


@pytest.mark.asyncio
async def test_missing_history_never_yields_receipt(receipt_client):
    from src.services.execution_closure import ClosureBlocked, ExecutionClosureVerifier

    client = receipt_client
    conv = uuid.uuid4()
    with pytest.raises(ClosureBlocked, match="history_unavailable"):
        await ExecutionClosureVerifier(client).inspect_tree(
            conv, f"conversation-{conv}", str(uuid.uuid4())
        )


@workflow.defn(name="ConversationWorkflow")
class SignalReceiptRoot:
    @workflow.run
    async def run(self, data: dict) -> None:
        return None

    @workflow.signal
    async def user_message(self, data: dict) -> None:
        pass


@pytest.mark.asyncio
async def test_lost_signal_replies_reconcile_reused_workflow_id_to_separate_runs(
    db, test_org, test_user, receipt_client
):
    from sqlalchemy import select
    from src.models.archive_control import ConversationExecution
    from src.models.conversation import Conversation
    from src.services.conversation_lifecycle import begin_operation
    from src.services.execution_closure import reconcile_signal_operation, observe_execution_tree

    conv = Conversation(org_id=test_org.id, user_id=test_user.id, model="test")
    db.add(conv)
    await db.flush()
    client = receipt_client
    operations = []
    run_ids = []
    async with Worker(client, task_queue=f"session-{conv.id}", workflows=[SignalReceiptRoot]):
        for _ in range(2):
            operation = await begin_operation(
                db, test_org.id, conv.id, kind="workflow_signal", operation_key=uuid.uuid4()
            )
            await db.commit()  # The durable intent exists before any Temporal RPC.
            handle = await client.start_workflow(
                SignalReceiptRoot.run,
                {"conversation_id": str(conv.id)},
                id=f"conversation-{conv.id}",
                task_queue=f"session-{conv.id}",
                start_signal="user_message",
                start_signal_args=[
                    {
                        "turn_id": str(operation.operation_key),
                        "lifecycle_operation_id": str(operation.id),
                    }
                ],
            )
            await handle.result()
            operations.append(operation)
            run_ids.append(uuid.UUID(handle.result_run_id))
        assert run_ids[0] != run_ids[1]
        for operation, run_id in zip(operations, run_ids, strict=True):
            # Simulate a lost RPC reply by leaving the accepted intent pending.
            assert operation.status == "pending"
            assert await reconcile_signal_operation(db, client, operation)
            assert operation.run_id == run_id
        rows = list(
            (
                await db.scalars(
                    select(ConversationExecution).where(
                        ConversationExecution.conversation_id == conv.id
                    )
                )
            ).all()
        )
        assert {row.run_id for row in rows} == set(run_ids)
        for row in rows:
            assert await observe_execution_tree(db, client, row)
            assert row.tree_complete and row.closed_at is not None
