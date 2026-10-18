"""Bounded exact Temporal provenance observer. History payloads are never persisted."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import WorkflowExecutionStatus

from src.models.archive_control import ConversationExecution
from src.services.interaction import TemporalExecutionVerifier


class ClosureBlocked(Exception):
    @property
    def retryable_registration(self) -> bool:
        # These are absence of a current proof, not a confirmed identity breach.
        # Archive observation remains blocked for every ClosureBlocked outcome.
        return str(self) in {"history_unavailable", "child_start_unsettled"}


@dataclass(frozen=True)
class ExecutionIdentity:
    namespace: str
    workflow_id: str
    run_id: UUID
    workflow_type: str
    task_queue: str
    parent_workflow_id: str | None
    parent_run_id: UUID | None
    initiated_event_id: int | None
    terminal_status: str | None
    closed_at: datetime | None

    @property
    def closed(self) -> bool:
        return self.terminal_status is not None and self.closed_at is not None


class ExecutionClosureVerifier(TemporalExecutionVerifier):
    """Reuse the interaction verifier's decoder; strengthen it with reciprocal history.

    Fetching the entire history via SDK fetch_history has unbounded memory. Here
    paging has both event and tree bounds. Exceeding a bound blocks, never truncates.
    """

    def __init__(self, client: Any) -> None:
        super().__init__(client)
        self._history_bytes = 0

    async def _input(self, start: Any) -> dict[str, Any]:
        try:
            return await super()._input(start)
        except Exception as exc:
            raise ClosureBlocked("history_input_unavailable") from exc

    async def _history(self, workflow_id: str, run_id: str) -> tuple[Any, list[Any]]:
        try:
            handle = self._client.get_workflow_handle(workflow_id, run_id=run_id)
            description = await handle.describe(rpc_timeout=timedelta(seconds=5))
            events = []
            history_bytes = 0
            async for event in handle.fetch_history_events(page_size=256, rpc_timeout=timedelta(seconds=5)):
                event_bytes = event.ByteSize()
                history_bytes += event_bytes
                self._history_bytes += event_bytes
                if history_bytes > 16 * 1024 * 1024 or self._history_bytes > 64 * 1024 * 1024:
                    raise ClosureBlocked("history_byte_limit")
                events.append(event)
                if len(events) > 100000:
                    raise ClosureBlocked("history_limit")
        except ClosureBlocked:
            raise
        except Exception as exc:
            raise ClosureBlocked("history_unavailable") from exc
        if not events:
            raise ClosureBlocked("history_unavailable")
        if description.id != workflow_id or str(description.run_id) != run_id:
            raise ClosureBlocked("execution_identity_mismatch")
        if not events[0].HasField("workflow_execution_started_event_attributes"):
            raise ClosureBlocked("history_start_missing")
        return description, events

    async def inspect_tree(
        self, conversation_id: UUID, workflow_id: str, run_id: str
    ) -> list[ExecutionIdentity]:
        self._history_bytes = 0
        result: list[ExecutionIdentity] = []
        seen: set[tuple[str, str]] = set()
        await self._visit(conversation_id, workflow_id, run_id, None, result, seen)
        return result

    async def _visit(
        self,
        conversation_id: UUID,
        workflow_id: str,
        run_id: str,
        parent: tuple[ExecutionIdentity, Any, int] | None,
        result: list[ExecutionIdentity],
        seen: set[tuple[str, str]],
    ) -> None:
        if (workflow_id, run_id) in seen or len(seen) >= 1000:
            raise ClosureBlocked("execution_tree_limit")
        seen.add((workflow_id, run_id))
        description, events = await self._history(workflow_id, run_id)
        start = events[0].workflow_execution_started_event_attributes
        payload = await self._input(start)
        namespace = self._client.namespace
        if (
            start.continued_execution_run_id
            or description.status == WorkflowExecutionStatus.CONTINUED_AS_NEW
        ):
            raise ClosureBlocked("continue_as_new_unreconciled")
        queue = f"session-{conversation_id}"
        if description.task_queue != queue or start.task_queue.name != queue:
            raise ClosureBlocked("execution_queue_mismatch")
        allowed = {
            "ConversationWorkflow",
            "AgentWorkflow",
            "MultiAgentWorkflow",
            "EvaluationWorkflow",
            "CodeSkillSessionWorkflow",
        }
        if (
            description.workflow_type not in allowed
            or description.workflow_type != start.workflow_type.name
        ):
            raise ClosureBlocked("unknown_execution_type")
        p = start.parent_workflow_execution
        if parent is None:
            if (
                description.workflow_type != "ConversationWorkflow"
                or workflow_id != f"conversation-{conversation_id}"
                or payload.get("conversation_id") != str(conversation_id)
                or p.workflow_id
                or p.run_id
            ):
                raise ClosureBlocked("root_identity_mismatch")
        else:
            owner, initiated, event_id = parent
            if (
                p.workflow_id,
                p.run_id,
                start.parent_workflow_namespace,
                start.parent_initiated_event_id,
            ) != (owner.workflow_id, str(owner.run_id), namespace, event_id):
                raise ClosureBlocked("parent_identity_mismatch")
            if (
                initiated.workflow_id != workflow_id
                or initiated.workflow_type.name != description.workflow_type
                or initiated.task_queue.name != queue
                or (initiated.namespace and initiated.namespace != namespace)
            ):
                raise ClosureBlocked("initiation_identity_mismatch")
            if list(initiated.input.payloads) != list(start.input.payloads):
                raise ClosureBlocked("child_input_mismatch")
            if description.workflow_type in {
                "EvaluationWorkflow",
                "CodeSkillSessionWorkflow",
            } and payload.get("conversation_id") != str(conversation_id):
                raise ClosureBlocked("child_owner_mismatch")
            if (
                description.workflow_type == "MultiAgentWorkflow"
                and workflow_id != f"{payload.get('run_id')}:orchestrator"
            ):
                raise ClosureBlocked("orchestrator_identity_mismatch")
            if description.workflow_type == "AgentWorkflow" and payload.get("workflow_run_id"):
                run = payload["workflow_run_id"]
                phase = payload.get("workflow_phase")
                expected = (
                    f"{run}:node:{payload.get('workflow_node_key')}"
                    if phase == "node"
                    else f"{run}:{phase}"
                )
                if (
                    owner.workflow_type != "MultiAgentWorkflow"
                    or owner.workflow_id != f"{run}:orchestrator"
                    or expected != workflow_id
                ):
                    raise ClosureBlocked("agent_identity_mismatch")
        closed = description.status not in (None, WorkflowExecutionStatus.RUNNING)
        identity = ExecutionIdentity(
            namespace,
            workflow_id,
            UUID(run_id),
            description.workflow_type,
            queue,
            p.workflow_id or None,
            UUID(p.run_id) if p.run_id else None,
            start.parent_initiated_event_id or None,
            description.status.name if closed else None,
            description.close_time if closed else None,
        )
        result.append(identity)
        initiated_by_id = {
            event.event_id: event.start_child_workflow_execution_initiated_event_attributes
            for event in events
            if event.HasField("start_child_workflow_execution_initiated_event_attributes")
        }
        settled_initiations = set()
        children = []
        for event in events:
            if event.HasField("start_child_workflow_execution_failed_event_attributes"):
                settled_initiations.add(
                    event.start_child_workflow_execution_failed_event_attributes.initiated_event_id
                )
            elif event.HasField("child_workflow_execution_started_event_attributes"):
                child = event.child_workflow_execution_started_event_attributes
                initiation = initiated_by_id.get(child.initiated_event_id)
                if (
                    initiation is None
                    or (child.namespace and child.namespace != namespace)
                    or child.workflow_type.name != initiation.workflow_type.name
                ):
                    raise ClosureBlocked("child_initiation_missing")
                settled_initiations.add(child.initiated_event_id)
                children.append((child, initiation))
        if set(initiated_by_id) != settled_initiations:
            raise ClosureBlocked("child_start_unsettled")
        for child, initiation in children:
            await self._visit(
                conversation_id,
                child.workflow_execution.workflow_id,
                child.workflow_execution.run_id,
                (identity, initiation, child.initiated_event_id),
                result,
                seen,
            )


async def observe_execution_tree(
    session: AsyncSession, client: Any, root: ConversationExecution
) -> bool:
    """Operator-only receipt updates. A failed observation preserves known identities."""
    if root.tree_complete:
        return True
    try:
        tree = await ExecutionClosureVerifier(client).inspect_tree(
            root.conversation_id, root.workflow_id, str(root.run_id)
        )
    except ClosureBlocked as exc:
        root.blocked_reason = str(exc)
        root.observed_at = datetime.now(timezone.utc)
        await session.flush()
        return False
    complete = all(identity.closed for identity in tree)
    for identity in tree:
        row = await session.scalar(
            select(ConversationExecution).where(
                ConversationExecution.namespace == identity.namespace,
                ConversationExecution.workflow_id == identity.workflow_id,
                ConversationExecution.run_id == identity.run_id,
            )
        )
        if row is None:
            row = ConversationExecution(
                org_id=root.org_id,
                conversation_id=root.conversation_id,
                generation=root.generation,
                **{
                    name: getattr(identity, name)
                    for name in (
                        "namespace",
                        "workflow_id",
                        "run_id",
                        "workflow_type",
                        "task_queue",
                        "parent_workflow_id",
                        "parent_run_id",
                        "initiated_event_id",
                    )
                },
            )
            session.add(row)
        elif (
            row.org_id != root.org_id
            or row.conversation_id != root.conversation_id
            or row.generation != root.generation
        ):
            raise ClosureBlocked("stored_identity_mismatch")
        row.terminal_status = identity.terminal_status
        row.closed_at = identity.closed_at
        row.observed_at = datetime.now(timezone.utc)
        # Tree-wide receipt: no child can be forgotten after retention expires.
        row.tree_complete = complete
        row.blocked_reason = None if complete else "execution_running"
    await session.flush()
    return complete


async def register_execution(
    session: AsyncSession,
    client: Any,
    *,
    org_id: UUID,
    conversation_id: UUID,
    generation: int,
    workflow_id: str,
    run_id: UUID,
) -> ConversationExecution:
    from sqlalchemy.dialects.postgresql import insert
    from src.services.conversation_lifecycle import lock_active_conversation

    verifier = ExecutionClosureVerifier(client)
    root_id, root_run = workflow_id, str(run_id)
    for _ in range(16):
        _, events = await verifier._history(root_id, root_run)
        parent = events[0].workflow_execution_started_event_attributes.parent_workflow_execution
        if not parent.workflow_id:
            break
        root_id, root_run = parent.workflow_id, parent.run_id
    else:
        raise ClosureBlocked("execution_tree_limit")
    root_payload = await verifier._input(events[0].workflow_execution_started_event_attributes)
    if root_payload.get("lifecycle_generation", 1) != generation:
        raise ClosureBlocked("execution_generation_mismatch")
    tree = await verifier.inspect_tree(conversation_id, root_id, root_run)
    identity = next(
        (row for row in tree if row.workflow_id == workflow_id and row.run_id == run_id), None
    )
    if identity is None:
        raise ClosureBlocked("execution_not_in_tree")
    await lock_active_conversation(session, org_id, conversation_id, generation=generation)
    # Insert only narrow immutable columns under the ordinary app role. Receipt
    # columns are writable exclusively by the independent operator observer.
    for observed in tree:
        values = {
            name: getattr(observed, name)
            for name in (
                "namespace",
                "workflow_id",
                "run_id",
                "workflow_type",
                "task_queue",
                "parent_workflow_id",
                "parent_run_id",
                "initiated_event_id",
            )
        }
        statement = (
            insert(ConversationExecution)
            .values(org_id=org_id, conversation_id=conversation_id, generation=generation, **values)
            .on_conflict_do_nothing(index_elements=["namespace", "workflow_id", "run_id"])
        )
        await session.execute(statement)
        existing = await session.scalar(
            select(ConversationExecution).where(
                ConversationExecution.namespace == observed.namespace,
                ConversationExecution.workflow_id == observed.workflow_id,
                ConversationExecution.run_id == observed.run_id,
            )
        )
        if (
            existing is None
            or existing.org_id != org_id
            or existing.conversation_id != conversation_id
            or existing.generation != generation
        ):
            raise ClosureBlocked("stored_identity_mismatch")
    row = await session.scalar(
        select(ConversationExecution).where(
            ConversationExecution.namespace == client.namespace,
            ConversationExecution.workflow_id == workflow_id,
            ConversationExecution.run_id == run_id,
        )
    )
    assert row is not None
    return row


async def reconcile_signal_operation(session: AsyncSession, client: Any, operation: Any) -> bool:
    """A signal receipt is the matching UUID in history, not latest-run guesswork."""
    from src.services.conversation_lifecycle import settle_operation

    verifier = ExecutionClosureVerifier(client)
    expected_id = f"conversation-{operation.conversation_id}"
    inspected = 0
    # Exact server-generated workflow ID. Reused IDs produce separate RunIDs.
    async for execution in client.list_workflows(
        query=f'WorkflowId = "{expected_id}"', page_size=100
    ):
        inspected += 1
        if inspected > 1000:
            raise ClosureBlocked("execution_discovery_limit")
        _, events = await verifier._history(expected_id, str(execution.run_id))
        if operation.kind == "workflow_start":
            payload = await verifier._input(events[0].workflow_execution_started_event_attributes)
            if payload.get("lifecycle_operation_id") == str(operation.id):
                await register_execution(
                    session,
                    client,
                    org_id=operation.org_id,
                    conversation_id=operation.conversation_id,
                    generation=operation.generation,
                    workflow_id=expected_id,
                    run_id=UUID(str(execution.run_id)),
                )
                await settle_operation(
                    session, operation, workflow_id=expected_id, run_id=UUID(str(execution.run_id))
                )
                return True
        for event in events:
            if not event.HasField("workflow_execution_signaled_event_attributes"):
                continue
            signal = event.workflow_execution_signaled_event_attributes
            if signal.signal_name != "user_message":
                continue
            decoded = await client.data_converter.decode(signal.input.payloads)
            if len(decoded) != 1 or not isinstance(decoded[0], dict):
                continue
            payload = decoded[0]
            if payload.get("lifecycle_operation_id") != str(operation.id) or payload.get(
                "turn_id"
            ) != str(operation.operation_key):
                continue
            await register_execution(
                session,
                client,
                org_id=operation.org_id,
                conversation_id=operation.conversation_id,
                generation=operation.generation,
                workflow_id=expected_id,
                run_id=UUID(str(execution.run_id)),
            )
            await settle_operation(
                session, operation, workflow_id=expected_id, run_id=UUID(str(execution.run_id))
            )
            return True
    return False


async def observe_pending_executions(client: Any, sessions: Any, batch_size: int = 100) -> int:
    """Invoke with an operator-role session factory; runs independently each minute."""
    from sqlalchemy import text
    from src.models.archive_control import ConversationOperation

    async with sessions() as discovery:
        roots = (
            await discovery.execute(
                text("SELECT * FROM discover_pending_conversation_executions(:limit)"),
                {"limit": batch_size},
            )
        ).all()
        operations = (
            await discovery.execute(
                text("SELECT * FROM discover_pending_conversation_operations(:limit)"),
                {"limit": batch_size},
            )
        ).all()
    observed = 0
    for org_id, operation_id in operations:
        async with sessions() as session:
            session.info["org_id"] = str(org_id)
            await session.execute(
                text("SELECT set_config('app.current_org_id',:org,true)"), {"org": str(org_id)}
            )
            operation = await session.get(ConversationOperation, operation_id)
            if operation is not None:
                try:
                    await reconcile_signal_operation(session, client, operation)
                except ClosureBlocked:
                    await session.rollback()
                    operation = await session.get(ConversationOperation, operation_id)
                    if operation is not None:
                        operation.updated_at = datetime.now(timezone.utc)
                        await session.commit()
                else:
                    operation.updated_at = datetime.now(timezone.utc)
                    await session.commit()
    for org_id, execution_id in roots:
        async with sessions() as session:
            session.info["org_id"] = str(org_id)
            await session.execute(
                text("SELECT set_config('app.current_org_id',:org,true)"), {"org": str(org_id)}
            )
            root = await session.get(ConversationExecution, execution_id)
            if root is not None:
                observed += int(await observe_execution_tree(session, client, root))
                await session.commit()
    return observed


async def start_idle_conversation(
    session: AsyncSession, client: Any, *, org_id: UUID, conversation_id: UUID, operation_key: UUID
) -> UUID:
    """Start an idle root with durable intent, without a synthetic user turn.

    Provision an authorized bound runner after intent commit. This is useful for exact
    isolated acceptance fixtures; it neither changes activity timestamps nor
    manufactures a terminal receipt. Closure still requires real observation.
    """
    from temporalio.common import WorkflowIDReusePolicy
    from src.services.conversation_lifecycle import begin_operation, settle_operation
    from src.workflows.conversation import ConversationInput, ConversationWorkflow

    operation = await begin_operation(
        session, org_id, conversation_id, kind="workflow_start", operation_key=operation_key
    )
    workflow_id = f"conversation-{conversation_id}"
    if operation.status == "settled":
        if operation.run_id is None or operation.workflow_id != workflow_id:
            raise ClosureBlocked("settled_start_identity_missing")
        return operation.run_id
    if operation.workflow_id is not None:
        if await reconcile_signal_operation(session, client, operation):
            await session.commit()
            assert operation.run_id is not None
            return operation.run_id
        raise ClosureBlocked("start_intent_unsettled")
    operation.workflow_id = workflow_id
    await session.commit()
    from src.models.data_identity import ConversationIdentity
    from src.services.session_manager import session_manager

    owner_id = await session.scalar(
        select(ConversationIdentity.user_id).where(
            ConversationIdentity.org_id == org_id, ConversationIdentity.id == conversation_id
        )
    )
    if owner_id is None:
        raise ClosureBlocked("conversation_owner_missing")
    await session_manager.get_or_create(
        str(conversation_id), str(owner_id), str(org_id), generation=operation.generation
    )
    handle = await client.start_workflow(
        ConversationWorkflow.run,
        ConversationInput(str(conversation_id), operation.generation, str(operation.id)),
        id=workflow_id,
        task_queue=f"session-{conversation_id}",
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )
    if handle.result_run_id is None:
        raise ClosureBlocked("exact_run_unavailable")
    run_id = UUID(handle.result_run_id)
    await register_execution(
        session,
        client,
        org_id=org_id,
        conversation_id=conversation_id,
        generation=operation.generation,
        workflow_id=workflow_id,
        run_id=run_id,
    )
    await settle_operation(session, operation, workflow_id=workflow_id, run_id=run_id)
    await session.commit()
    return run_id
