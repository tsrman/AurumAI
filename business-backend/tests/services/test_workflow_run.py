import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from src.core.authorization_types import AuthorizationSubject
from src.models.agent_definition import AgentDefinition
from src.models.conversation import Conversation
from src.models.message import Message
from src.models.workflow_definition import WorkflowDefinition, WorkflowEdge, WorkflowNode
from src.models.workflow_run import WorkflowNodeRun, WorkflowRunStatus
from src.repositories.workflow_run_repository import (
    WorkflowNodeTransitionRejected,
    WorkflowOutputLimitExceeded,
    WorkflowRunRepository,
    WorkflowTransitionRejected,
)
from src.services.resource_registry import ResourceRegistry
from src.services.agent_authorization import AgentAuthorizationService
from src.services.workflow_authorization import WorkflowAuthorizationService
from src.services.workflow_run import WorkflowRunNotFound, WorkflowRunService
from src.services.workflow_graph import WorkflowGraphError


async def test_template_copy_run_rechecks_revoked_dependency_without_snapshot_permissions(
    db, test_org, test_user, template_source
) -> None:
    """A copied definition must use the ordinary runtime's live authorization."""
    from src.core.authorization_types import ResourceType
    from src.services.model_catalog import GatewayCatalogSnapshot, GatewayCatalogStatus
    from src.schemas.workflow_template import TemplateCaptureRequest, TemplateInstantiateRequest
    from src.services.workflow_template import WorkflowTemplateService
    from src.services.workflow_authorization import WorkflowStepAccessDenied
    from tests.services.test_workflow_authorization import grant

    async def template_catalog() -> GatewayCatalogSnapshot:
        return GatewayCatalogSnapshot(
            GatewayCatalogStatus.AVAILABLE, ({"id": "provider/model-v1"},)
        )

    actor = AuthorizationSubject(test_user.id, test_org.id, "org_admin")
    templates = WorkflowTemplateService(catalog=template_catalog)
    template = await templates.capture(
        db,
        actor,
        TemplateCaptureRequest(
            source_workflow_id=template_source.id,
            source_workflow_version=1,
            name="Runtime regression template",
        ),
    )
    copy = await templates.instantiate(
        db, actor, template.id, TemplateInstantiateRequest(version=1, name="Runtime copy")
    )
    _, conversation, message, turn_id = await create_runtime_graph(db, test_org, test_user)
    dependency_grant = grant(
        test_org.id, test_user.id, ResourceType.AGENT_DEFINITION, str(copy.agent_ids[0])
    )
    db.add_all(
        [
            grant(test_org.id, test_user.id, ResourceType.WORKFLOW, str(copy.workflow.id)),
            dependency_grant,
            grant(test_org.id, test_user.id, ResourceType.MODEL, "provider/model-v1"),
        ]
    )
    await db.commit()
    member = AuthorizationSubject(test_user.id, test_org.id, "member")
    run = await run_service().create_authorized_run(
        db, member, copy.workflow.id, conversation.id, turn_id, message.id
    )
    await db.commit()
    authorization = WorkflowAuthorizationService(registry=ResourceRegistry(model_catalog=catalog))
    await authorization.require_snapshot_step_executable(
        db, member, run, phase="supervisor_prepare", node_key=None
    )
    snapshot = run.execution_snapshot
    assert snapshot["nodes"][0]["requiresConfirmation"] is True
    assert snapshot["supervisor"]["agentDefinitionId"] == str(copy.agent_ids[0])

    def assert_no_permissions(value):
        if isinstance(value, dict):
            assert (
                not {"permissions", "grants", "capabilities", "role", "canRead", "canWrite"}
                & value.keys()
            )
            for child in value.values():
                assert_no_permissions(child)
        elif isinstance(value, list):
            for child in value:
                assert_no_permissions(child)

    assert_no_permissions(snapshot)
    await db.delete(dependency_grant)
    await db.commit()
    with pytest.raises(WorkflowStepAccessDenied):
        await authorization.require_snapshot_step_executable(
            db, member, run, phase="node", node_key="research"
        )
    assert run.execution_snapshot == snapshot


async def catalog() -> list[dict[str, object]]:
    return [{"id": "provider/model-v1"}]


def run_service(interactions=None) -> WorkflowRunService:  # noqa: ANN001
    registry = ResourceRegistry(model_catalog=catalog)
    return WorkflowRunService(
        authorization=WorkflowAuthorizationService(registry=registry),
        agent_authorization=AgentAuthorizationService(registry=registry),
        interactions=interactions,
    )


class _Interactions:
    def __init__(self) -> None:
        self.calls = []

    async def finish_turn(
        self,
        session,
        org_id,
        owner_id,
        conversation_id,
        turn_id,
        *,
        status,  # noqa: ANN001
    ) -> None:
        self.calls.append((org_id, owner_id, conversation_id, turn_id, status))


async def create_runtime_graph(db, test_org, test_user, *, requires_confirmation: bool = False):
    supervisor = AgentDefinition(
        org_id=test_org.id,
        name=f"Supervisor-{uuid4()}",
        model_id="provider/model-v1",
        system_prompt="Plan and synthesize",
    )
    worker = AgentDefinition(
        org_id=test_org.id,
        name=f"Worker-{uuid4()}",
        model_id="provider/model-v1",
        system_prompt="Research",
    )
    db.add_all([supervisor, worker])
    await db.flush()
    definition = WorkflowDefinition(
        org_id=test_org.id,
        name=f"Runtime-{uuid4()}",
        supervisor_agent_definition_id=supervisor.id,
        max_parallelism=2,
    )
    db.add(definition)
    await db.flush()
    db.add(
        WorkflowNode(
            org_id=test_org.id,
            workflow_definition_id=definition.id,
            node_key="research",
            display_name="Research",
            agent_definition_id=worker.id,
            instruction="Find evidence",
            requires_confirmation=requires_confirmation,
        )
    )
    conversation = Conversation(
        org_id=test_org.id,
        user_id=test_user.id,
        title="Runtime",
        model="provider/model-v1",
    )
    db.add(conversation)
    await db.flush()
    turn_id = uuid4()
    message = Message(
        org_id=test_org.id,
        conversation_id=conversation.id,
        turn_id=turn_id,
        user_id=test_user.id,
        role="user",
        content="Compare reports",
    )
    db.add(message)
    await db.commit()
    return definition, conversation, message, turn_id


async def test_create_run_materializes_immutable_snapshot_and_phase_rows(
    db, test_org, test_user
) -> None:
    definition, conversation, message, turn_id = await create_runtime_graph(db, test_org, test_user)
    service = run_service()

    run = await service.create_authorized_run(
        db,
        AuthorizationSubject(test_user.id, test_org.id, "org_admin"),
        definition.id,
        conversation.id,
        turn_id,
        message.id,
    )
    await db.commit()
    phase_rows = list(
        (
            await db.execute(
                select(WorkflowNodeRun)
                .where(WorkflowNodeRun.workflow_run_id == run.id)
                .order_by(WorkflowNodeRun.phase_key)
            )
        ).scalars()
    )

    assert run.execution_snapshot["userRequest"] == "Compare reports"
    assert run.execution_snapshot["nodes"][0]["nodeKey"] == "research"
    assert [row.phase_key for row in phase_rows] == [
        "research",
        "supervisor_prepare",
        "supervisor_synthesize",
    ]


async def test_run_transitions_are_monotonic_and_idempotent(db, test_org, test_user) -> None:
    definition, conversation, message, turn_id = await create_runtime_graph(db, test_org, test_user)
    service = run_service()
    run = await service.create_authorized_run(
        db,
        AuthorizationSubject(test_user.id, test_org.id, "org_admin"),
        definition.id,
        conversation.id,
        turn_id,
        message.id,
    )
    repository = WorkflowRunRepository()

    for status in (
        WorkflowRunStatus.PLANNING,
        WorkflowRunStatus.RUNNING,
        WorkflowRunStatus.SYNTHESIZING,
        WorkflowRunStatus.COMPLETED,
    ):
        transitioned = await repository.transition(db, run.id, status, None)
        repeated = await repository.transition(db, run.id, status, None)
        assert transitioned.status == status.value
        assert repeated.status == status.value

    with pytest.raises(WorkflowTransitionRejected):
        await repository.transition(db, run.id, WorkflowRunStatus.RUNNING, None)


async def test_terminal_workflow_transition_finishes_exact_interaction_turn(
    db, test_org, test_user
) -> None:
    definition, conversation, message, turn_id = await create_runtime_graph(db, test_org, test_user)
    interactions = _Interactions()
    service = run_service(interactions)
    run = await service.create_authorized_run(
        db,
        AuthorizationSubject(test_user.id, test_org.id, "org_admin"),
        definition.id,
        conversation.id,
        turn_id,
        message.id,
    )
    for status in (WorkflowRunStatus.PLANNING, WorkflowRunStatus.FAILED):
        await service.transition(
            db, run.id, status, "node_failed" if status.value == "failed" else None
        )
    assert interactions.calls == [(test_org.id, test_user.id, conversation.id, turn_id, "failed")]


async def test_cancel_request_finishes_exact_interaction_turn(db, test_org, test_user) -> None:
    definition, conversation, message, turn_id = await create_runtime_graph(db, test_org, test_user)
    interactions = _Interactions()
    service = run_service(interactions)
    run = await service.create_authorized_run(
        db,
        AuthorizationSubject(test_user.id, test_org.id, "org_admin"),
        definition.id,
        conversation.id,
        turn_id,
        message.id,
    )

    cancelled = await service.request_cancel(
        db,
        AuthorizationSubject(test_user.id, test_org.id, "member"),
        run.id,
    )

    assert cancelled.status == WorkflowRunStatus.CANCELLED.value
    assert interactions.calls == [
        (test_org.id, test_user.id, conversation.id, turn_id, "cancelled")
    ]


async def test_node_output_limits_and_expired_redaction(db, test_org, test_user) -> None:
    definition, conversation, message, turn_id = await create_runtime_graph(db, test_org, test_user)
    service = run_service()
    run = await service.create_authorized_run(
        db,
        AuthorizationSubject(test_user.id, test_org.id, "org_admin"),
        definition.id,
        conversation.id,
        turn_id,
        message.id,
    )
    repository = WorkflowRunRepository()

    accepted = await repository.record_node_result(
        db, run.id, "research", "completed", "x" * 65_536, None
    )
    with pytest.raises(WorkflowOutputLimitExceeded):
        await repository.record_node_result(db, run.id, "research", "completed", "x" * 65_537, None)
    db.add_all(
        [
            WorkflowNodeRun(
                org_id=test_org.id,
                workflow_run_id=run.id,
                phase_key=f"aggregate-{index}",
                agent_definition_id=accepted.agent_definition_id,
                agent_name=accepted.agent_name,
            )
            for index in range(6)
        ]
    )
    await db.flush()
    for phase_key in (
        "supervisor_prepare",
        "supervisor_synthesize",
        "aggregate-0",
        "aggregate-1",
        "aggregate-2",
        "aggregate-3",
        "aggregate-4",
    ):
        await repository.record_node_result(db, run.id, phase_key, "completed", "y" * 65_536, None)
    with pytest.raises(WorkflowOutputLimitExceeded) as aggregate_error:
        await repository.record_node_result(db, run.id, "aggregate-5", "completed", "z", None)
    rows = await repository.list_node_runs(db, run.id)
    await repository.record_node_result(db, run.id, "research", "completed", "replacement", None)
    repeated_rows = await repository.list_node_runs(db, run.id)
    accepted.diagnostic_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.flush()

    diagnostic = await service.get_admin_diagnostic(db, test_org.id, run.id)
    cleared = await repository.clear_expired_diagnostics(db, datetime.now(UTC))
    research_diagnostic = next(item for item in diagnostic.nodes if item.phase_key == "research")

    assert research_diagnostic.output is None
    assert research_diagnostic.output_expired is True
    assert cleared == 1
    assert aggregate_error.value.code == "aggregate_output_limit"
    assert len(rows) == len(repeated_rows) == 9


async def test_node_transitions_are_monotonic_and_idempotent(db, test_org, test_user) -> None:
    definition, conversation, message, turn_id = await create_runtime_graph(db, test_org, test_user)
    service = run_service()
    run = await service.create_authorized_run(
        db,
        AuthorizationSubject(test_user.id, test_org.id, "org_admin"),
        definition.id,
        conversation.id,
        turn_id,
        message.id,
    )
    repository = WorkflowRunRepository()

    running = await repository.record_node_result(db, run.id, "research", "running", None, None)
    repeated = await repository.record_node_result(db, run.id, "research", "running", None, None)
    completed = await repository.record_node_result(
        db, run.id, "research", "completed", "done", None
    )

    assert running.attempt_count == repeated.attempt_count == 1
    assert completed.status == "completed"
    with pytest.raises(WorkflowNodeTransitionRejected):
        await repository.record_node_result(db, run.id, "research", "running", None, None)


async def test_node_may_transition_to_skipped_only_from_pending(db, test_org, test_user) -> None:
    definition, conversation, message, turn_id = await create_runtime_graph(db, test_org, test_user)
    service = run_service()
    run = await service.create_authorized_run(
        db,
        AuthorizationSubject(test_user.id, test_org.id, "org_admin"),
        definition.id,
        conversation.id,
        turn_id,
        message.id,
    )
    repository = WorkflowRunRepository()

    skipped = await repository.record_node_result(db, run.id, "research", "skipped", None, None)
    assert skipped.status == "skipped"

    with pytest.raises(WorkflowNodeTransitionRejected):
        await repository.record_node_result(db, run.id, "research", "running", None, None)


async def test_run_creation_revalidates_persisted_graph_before_snapshot(
    db, test_org, test_user
) -> None:
    definition, conversation, message, turn_id = await create_runtime_graph(db, test_org, test_user)
    research = await db.scalar(
        select(WorkflowNode).where(
            WorkflowNode.workflow_definition_id == definition.id,
            WorkflowNode.node_key == "research",
        )
    )
    assert research is not None
    review = WorkflowNode(
        org_id=test_org.id,
        workflow_definition_id=definition.id,
        node_key="review",
        display_name="Review",
        agent_definition_id=research.agent_definition_id,
        instruction="Review evidence",
    )
    db.add(review)
    await db.flush()
    db.add_all(
        [
            WorkflowEdge(
                org_id=test_org.id,
                workflow_definition_id=definition.id,
                source_node_id=research.id,
                target_node_id=review.id,
            ),
            WorkflowEdge(
                org_id=test_org.id,
                workflow_definition_id=definition.id,
                source_node_id=review.id,
                target_node_id=research.id,
            ),
        ]
    )
    await db.commit()

    with pytest.raises(WorkflowGraphError) as caught:
        await run_service().create_authorized_run(
            db,
            AuthorizationSubject(test_user.id, test_org.id, "org_admin"),
            definition.id,
            conversation.id,
            turn_id,
            message.id,
        )

    assert caught.value.code == "cycle"


async def test_run_creation_preserves_loop_back_edges_in_the_snapshot(
    db, test_org, test_user
) -> None:
    """create_authorized_run's own edge reconstruction (for the second,
    run-time validate_workflow_graph pass) must carry condition/is_loop_back/
    max_iterations through — omitting them would make every run of a
    workflow containing a loop 409 as a plain structural cycle, since an
    edge that silently defaults to is_loop_back=False here is evaluated as
    an ordinary (non-loop) edge.
    """
    definition, conversation, message, turn_id = await create_runtime_graph(db, test_org, test_user)
    research = await db.scalar(
        select(WorkflowNode).where(
            WorkflowNode.workflow_definition_id == definition.id,
            WorkflowNode.node_key == "research",
        )
    )
    assert research is not None
    db.add(
        WorkflowEdge(
            org_id=test_org.id,
            workflow_definition_id=definition.id,
            source_node_id=research.id,
            target_node_id=research.id,
            condition="on_failure",
            is_loop_back=True,
            max_iterations=4,
        )
    )
    await db.commit()

    run = await run_service().create_authorized_run(
        db,
        AuthorizationSubject(test_user.id, test_org.id, "org_admin"),
        definition.id,
        conversation.id,
        turn_id,
        message.id,
    )

    edges = run.execution_snapshot["edges"]
    assert len(edges) == 1
    assert edges[0]["condition"] == "on_failure"
    assert edges[0]["isLoopBack"] is True
    assert edges[0]["maxIterations"] == 4
    loop_bodies = run.execution_snapshot["loopBodies"]
    assert len(loop_bodies) == 1
    assert loop_bodies[0]["nodeKeys"] == ["research"]
    assert loop_bodies[0]["maxIterations"] == 4


async def test_retention_reaper_continues_after_failed_sweep(monkeypatch) -> None:
    service = run_service()
    sweeps = 0
    sleeps = 0

    async def clear(now):  # noqa: ANN001
        nonlocal sweeps
        sweeps += 1
        if sweeps == 1:
            raise RuntimeError("temporary database failure")
        return 1

    async def sleep(seconds):  # noqa: ANN001
        nonlocal sleeps
        sleeps += 1
        assert seconds > 0
        if sleeps == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(service, "clear_expired_diagnostics", clear)
    monkeypatch.setattr("src.services.workflow_run.asyncio.sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await service.run_retention_reaper()

    assert sweeps == 2
    assert sleeps == 2


async def test_expired_diagnostics_sweep_works_under_the_rls_runtime_role(
    db, test_org, test_user
) -> None:
    """The reaper is cross-tenant, so it has no org context to set.  Pin that
    it still clears rows when it runs as aurum_ai_app — a plain UPDATE
    silently matched zero rows there while passing every superuser test."""
    definition, conversation, message, turn_id = await create_runtime_graph(db, test_org, test_user)
    service = run_service()
    run = await service.create_authorized_run(
        db,
        AuthorizationSubject(test_user.id, test_org.id, "org_admin"),
        definition.id,
        conversation.id,
        turn_id,
        message.id,
    )
    repository = WorkflowRunRepository()
    node = await repository.record_node_result(
        db, run.id, "research", "completed", "leaked-node-output", None
    )
    node.diagnostic_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.flush()

    await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
    try:
        cleared = await repository.clear_expired_diagnostics(db, datetime.now(UTC))
    finally:
        await db.execute(text("RESET ROLE"))

    refreshed = await db.scalar(select(WorkflowNodeRun).where(WorkflowNodeRun.id == node.id))
    await db.refresh(refreshed)

    assert cleared == 1
    assert refreshed is not None
    assert refreshed.output is None
    assert refreshed.internal_error is None


async def test_request_decision_accepts_a_confirmation_gated_node(db, test_org, test_user) -> None:
    definition, conversation, message, turn_id = await create_runtime_graph(
        db, test_org, test_user, requires_confirmation=True
    )
    service = run_service()
    subject = AuthorizationSubject(test_user.id, test_org.id, "org_admin")
    run = await service.create_authorized_run(
        db, subject, definition.id, conversation.id, turn_id, message.id
    )
    await db.commit()

    decided = await service.request_decision(db, subject, run.id, "research")

    assert decided.id == run.id


async def test_request_decision_rejects_node_that_never_required_confirmation(
    db, test_org, test_user
) -> None:
    definition, conversation, message, turn_id = await create_runtime_graph(
        db, test_org, test_user, requires_confirmation=False
    )
    service = run_service()
    subject = AuthorizationSubject(test_user.id, test_org.id, "org_admin")
    run = await service.create_authorized_run(
        db, subject, definition.id, conversation.id, turn_id, message.id
    )
    await db.commit()

    with pytest.raises(WorkflowRunNotFound):
        await service.request_decision(db, subject, run.id, "research")


async def test_request_decision_is_owner_scoped(db, test_org, test_user) -> None:
    definition, conversation, message, turn_id = await create_runtime_graph(
        db, test_org, test_user, requires_confirmation=True
    )
    service = run_service()
    owner = AuthorizationSubject(test_user.id, test_org.id, "org_admin")
    run = await service.create_authorized_run(
        db, owner, definition.id, conversation.id, turn_id, message.id
    )
    await db.commit()

    someone_else = AuthorizationSubject(uuid4(), test_org.id, "member")
    with pytest.raises(WorkflowRunNotFound):
        await service.request_decision(db, someone_else, run.id, "research")


async def test_request_decision_rejects_unknown_node_key(db, test_org, test_user) -> None:
    definition, conversation, message, turn_id = await create_runtime_graph(
        db, test_org, test_user, requires_confirmation=True
    )
    service = run_service()
    subject = AuthorizationSubject(test_user.id, test_org.id, "org_admin")
    run = await service.create_authorized_run(
        db, subject, definition.id, conversation.id, turn_id, message.id
    )
    await db.commit()

    with pytest.raises(WorkflowRunNotFound):
        await service.request_decision(db, subject, run.id, "not-a-real-node")
