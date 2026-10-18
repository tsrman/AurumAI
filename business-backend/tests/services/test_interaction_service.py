import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from src.core.authorization_types import AuthorizationSubject
from src.core.capability_catalog import Capability
from src.models.agent_definition import AgentDefinition
from src.models.conversation import Conversation
from src.models.interaction import InteractionTurn, UserQuestion
from src.models.message import Message
from src.models.workflow_definition import WorkflowDefinition
from src.models.workflow_run import WorkflowRun
from src.schemas.interaction import (
    AnswerRequest,
    ExecutionRegistrationRequest,
    QuestionCreateRequest,
    QuestionRequest,
    TurnLimits,
)


class _Authorization:
    def __init__(self) -> None:
        self.denied = False
        self.denied_capability: Capability | None = None
        self.calls = []

    async def require_capability(self, session, subject, capability):  # noqa: ANN001
        self.calls.append((subject, capability))
        if self.denied or capability is self.denied_capability:
            from src.services.authorization import CapabilityAccessDenied

            raise CapabilityAccessDenied(capability)


class _ModelAuthorization:
    def __init__(self) -> None:
        self.denied = False
        self.calls = []

    async def __call__(self, session, org_id, model_id, **kwargs):  # noqa: ANN001
        self.calls.append((org_id, model_id, kwargs))
        if self.denied:
            raise HTTPException(status_code=403, detail="Model not allowed")


class _WorkflowAuthorization:
    def __init__(self) -> None:
        self.denied = False
        self.calls = []

    async def require_snapshot_step_executable(
        self,
        session,
        subject,
        run,
        *,
        phase,
        node_key,  # noqa: ANN001
    ):
        self.calls.append((subject, run.id, phase, node_key))
        if self.denied:
            from src.services.workflow_authorization import WorkflowStepAccessDenied

            raise WorkflowStepAccessDenied(run.id)
        return SimpleNamespace(run_id=run.id, phase=phase, node_key=node_key)


class _Verifier:
    def __init__(self) -> None:
        self.calls = []

    async def verify(self, turn, data):  # noqa: ANN001
        self.calls.append((turn, data))


@pytest.fixture
async def interaction_fixture(db, test_org, test_user):
    conversation = Conversation(
        org_id=test_org.id, user_id=test_user.id, model="provider/model", status="active"
    )
    db.add(conversation)
    await db.flush()
    turn = InteractionTurn(
        org_id=test_org.id,
        conversation_id=conversation.id,
        owner_id=test_user.id,
        limits=TurnLimits().model_dump(),
    )
    db.add(turn)
    await db.flush()
    return SimpleNamespace(
        db=db,
        org=test_org.id,
        owner=test_user.id,
        conversation=conversation,
        turn=turn,
    )


def _service():
    from src.services.interaction import InteractionService

    auth = _Authorization()
    models = _ModelAuthorization()
    workflows = _WorkflowAuthorization()
    verifier = _Verifier()
    service = InteractionService(
        authorization=auth,
        model_authorizer=models,
        workflow_authorization=workflows,
        execution_verifier=verifier,
    )
    return service, auth, models, workflows, verifier


def _registration(conversation_id: UUID) -> ExecutionRegistrationRequest:
    return ExecutionRegistrationRequest(
        workflow_id=f"conversation-{conversation_id}",
        run_id=uuid4(),
        task_queue=f"session-{conversation_id}",
        phase="ordinary",
    )


async def _execution(fixture, service):  # noqa: ANN001
    subject = AuthorizationSubject(fixture.owner, fixture.org)
    return await service.register_execution(
        fixture.db,
        subject,
        fixture.conversation.id,
        fixture.turn.id,
        _registration(fixture.conversation.id),
    )


async def _question(fixture, service):  # noqa: ANN001
    execution = await _execution(fixture, service)
    return await service.create_question(
        fixture.db,
        AuthorizationSubject(fixture.owner, fixture.org),
        fixture.conversation.id,
        execution.id,
        QuestionCreateRequest(invocation_id=uuid4(), question=QuestionRequest(text="City?")),
    )


async def test_registration_is_owner_bound_verified_and_idempotent(interaction_fixture) -> None:
    service, auth, models, _, verifier = _service()
    first = await _execution(interaction_fixture, service)
    second = await service.register_execution(
        interaction_fixture.db,
        AuthorizationSubject(interaction_fixture.owner, interaction_fixture.org),
        interaction_fixture.conversation.id,
        interaction_fixture.turn.id,
        _registration(interaction_fixture.conversation.id).model_copy(
            update={"run_id": first.run_id}
        ),
    )
    assert second.id == first.id
    assert len(verifier.calls) == 2
    assert auth.calls[-1][1] is Capability.CHAT_SEND
    assert models.calls[-1][1] == "provider/model"

    for subject, conversation_id in [
        (
            AuthorizationSubject(uuid4(), interaction_fixture.org, "org_admin"),
            interaction_fixture.conversation.id,
        ),
        (AuthorizationSubject(interaction_fixture.owner, interaction_fixture.org), uuid4()),
        (
            AuthorizationSubject(interaction_fixture.owner, uuid4()),
            interaction_fixture.conversation.id,
        ),
    ]:
        from src.services.interaction import InteractionNotFound

        with pytest.raises(InteractionNotFound):
            await service.register_execution(
                interaction_fixture.db,
                subject,
                conversation_id,
                interaction_fixture.turn.id,
                _registration(interaction_fixture.conversation.id),
            )


async def test_web_gate_rechecks_live_target_and_distinct_capability(
    interaction_fixture,
) -> None:
    from src.services.interaction import (
        InteractionAccessDenied,
        InteractionConflict,
        InteractionNotFound,
    )

    service, auth, models, _, _ = _service()
    execution = await _execution(interaction_fixture, service)
    subject = AuthorizationSubject(interaction_fixture.owner, interaction_fixture.org)
    auth.calls.clear()
    models.calls.clear()

    await service.authorize_web_operation(
        interaction_fixture.db,
        subject,
        interaction_fixture.conversation.id,
        interaction_fixture.turn.id,
        execution.id,
    )

    assert [capability for _, capability in auth.calls] == [
        Capability.CHAT_SEND,
        Capability.WEB_ACCESS,
    ]
    assert models.calls[-1][1] == "provider/model"

    auth.denied_capability = Capability.WEB_ACCESS
    with pytest.raises(InteractionAccessDenied):
        await service.authorize_web_operation(
            interaction_fixture.db,
            subject,
            interaction_fixture.conversation.id,
            interaction_fixture.turn.id,
            execution.id,
        )

    with pytest.raises(InteractionNotFound):
        await service.authorize_web_operation(
            interaction_fixture.db,
            subject,
            interaction_fixture.conversation.id,
            uuid4(),
            execution.id,
        )

    auth.denied_capability = None
    interaction_fixture.turn.status = "failed"
    await interaction_fixture.db.flush()
    with pytest.raises(InteractionConflict):
        await service.authorize_web_operation(
            interaction_fixture.db,
            subject,
            interaction_fixture.conversation.id,
            interaction_fixture.turn.id,
            execution.id,
        )


async def test_create_and_answer_require_live_access_without_losing_question(
    interaction_fixture,
) -> None:
    from src.services.interaction import InteractionAccessDenied

    service, auth, models, _, _ = _service()
    question = await _question(interaction_fixture, service)
    saved_question = await interaction_fixture.db.get(UserQuestion, question.question_id)
    assert saved_question is not None
    auth.denied = True
    with pytest.raises(InteractionAccessDenied):
        await service.answer_question(
            interaction_fixture.db,
            AuthorizationSubject(interaction_fixture.owner, interaction_fixture.org),
            interaction_fixture.conversation.id,
            question.question_id,
            uuid4(),
            AnswerRequest(text="Bangkok"),
        )
    saved = await interaction_fixture.db.get(UserQuestion, question.question_id)
    assert saved is not None and saved.status == "pending"

    auth.denied = False
    models.denied = True
    with pytest.raises(InteractionAccessDenied):
        await service.create_question(
            interaction_fixture.db,
            AuthorizationSubject(interaction_fixture.owner, interaction_fixture.org),
            interaction_fixture.conversation.id,
            saved_question.execution_id,
            QuestionCreateRequest(invocation_id=uuid4(), question=QuestionRequest(text="Another?")),
        )


async def test_workflow_execution_registration_rechecks_snapshot_dependencies(
    interaction_fixture,
) -> None:
    from src.services.interaction import InteractionAccessDenied

    service, _, _, workflows, verifier = _service()
    fixture = interaction_fixture
    agent = AgentDefinition(
        org_id=fixture.org,
        created_by=fixture.owner,
        name=f"Interaction agent {uuid4()}",
        model_id="provider/model",
        system_prompt="Answer",
    )
    fixture.db.add(agent)
    await fixture.db.flush()
    definition = WorkflowDefinition(
        org_id=fixture.org,
        created_by=fixture.owner,
        name=f"Interaction workflow {uuid4()}",
        supervisor_agent_definition_id=agent.id,
    )
    fixture.db.add(definition)
    await fixture.db.flush()
    source = Message(
        org_id=fixture.org,
        conversation_id=fixture.conversation.id,
        turn_id=fixture.turn.id,
        user_id=fixture.owner,
        role="user",
        content="Research",
        model="provider/model",
    )
    fixture.db.add(source)
    await fixture.db.flush()
    run = WorkflowRun(
        org_id=fixture.org,
        workflow_definition_id=definition.id,
        workflow_definition_version=1,
        initiating_user_id=fixture.owner,
        conversation_id=fixture.conversation.id,
        turn_id=fixture.turn.id,
        source_message_id=source.id,
        temporal_workflow_id=f"interaction-{uuid4()}",
        execution_snapshot={
            "supervisor": {"agentDefinitionId": str(agent.id)},
            "nodes": [
                {
                    "nodeKey": "research",
                    "agent": {"agentDefinitionId": str(agent.id)},
                }
            ],
        },
    )
    fixture.db.add(run)
    await fixture.db.flush()
    fixture.turn.workflow_run_id = run.id
    await fixture.db.flush()
    workflows.denied = True

    with pytest.raises(InteractionAccessDenied):
        await service.register_execution(
            fixture.db,
            AuthorizationSubject(fixture.owner, fixture.org),
            fixture.conversation.id,
            fixture.turn.id,
            ExecutionRegistrationRequest(
                workflow_id=f"{run.id}:node:research",
                run_id=uuid4(),
                task_queue=f"session-{fixture.conversation.id}",
                phase="node",
                node_key="research",
                agent_definition_id=agent.id,
            ),
        )

    assert workflows.calls == [
        (
            AuthorizationSubject(fixture.owner, fixture.org),
            run.id,
            "node",
            "research",
        )
    ]
    assert verifier.calls == []


async def test_consume_rechecks_access_and_is_idempotent(interaction_fixture) -> None:
    from src.services.interaction import InteractionAccessDenied

    service, auth, _, _, _ = _service()
    subject = AuthorizationSubject(interaction_fixture.owner, interaction_fixture.org)
    question = await _question(interaction_fixture, service)
    answered = await service.answer_question(
        interaction_fixture.db,
        subject,
        interaction_fixture.conversation.id,
        question.question_id,
        uuid4(),
        AnswerRequest(text="Bangkok"),
    )
    assert answered.answer == AnswerRequest(text="Bangkok")
    auth.denied = True
    with pytest.raises(InteractionAccessDenied):
        await service.consume_question(
            interaction_fixture.db,
            subject,
            interaction_fixture.conversation.id,
            question.question_id,
        )
    saved = await interaction_fixture.db.get(UserQuestion, question.question_id)
    assert saved is not None and saved.status == "answered" and saved.consumed_at is None

    auth.denied = False
    first = await service.consume_question(
        interaction_fixture.db, subject, interaction_fixture.conversation.id, question.question_id
    )
    second = await service.consume_question(
        interaction_fixture.db, subject, interaction_fixture.conversation.id, question.question_id
    )
    assert first == second == AnswerRequest(text="Bangkok")
    assert (await interaction_fixture.db.get(UserQuestion, question.question_id)).consumed_at


async def test_list_and_cancel_are_strict_owner_only(interaction_fixture) -> None:
    from src.services.interaction import InteractionNotFound

    service, _, _, _, _ = _service()
    await _question(interaction_fixture, service)
    owner = AuthorizationSubject(interaction_fixture.owner, interaction_fixture.org)
    listed = await service.list_questions(
        interaction_fixture.db, owner, interaction_fixture.conversation.id
    )
    assert len(listed) == 1 and listed[0].text == "City?"
    with pytest.raises(InteractionNotFound):
        await service.list_questions(
            interaction_fixture.db,
            AuthorizationSubject(uuid4(), interaction_fixture.org, "org_admin"),
            interaction_fixture.conversation.id,
        )
    targets = await service.cancel_turn(
        interaction_fixture.db,
        owner,
        interaction_fixture.conversation.id,
        interaction_fixture.turn.id,
    )
    assert [(item.workflow_id, item.run_id) for item in targets]
    assert interaction_fixture.turn.status == "cancelled"


async def test_turn_snapshots_are_owner_only_and_survive_terminal_reconnect(
    interaction_fixture,
) -> None:
    from src.services.interaction import InteractionNotFound

    service, _, _, _, _ = _service()
    subject = AuthorizationSubject(interaction_fixture.owner, interaction_fixture.org)
    question = await _question(interaction_fixture, service)

    active = await service.get_active_turn_snapshot(
        interaction_fixture.db, subject, interaction_fixture.conversation.id
    )
    assert active is not None
    assert active.turn_id == interaction_fixture.turn.id
    assert [(item.question_id, item.status, item.version) for item in active.questions] == [
        (question.question_id, "pending", 1)
    ]

    await service.answer_question(
        interaction_fixture.db,
        subject,
        interaction_fixture.conversation.id,
        question.question_id,
        uuid4(),
        AnswerRequest(text="Bangkok"),
    )
    await service.finish_turn(
        interaction_fixture.db,
        interaction_fixture.org,
        interaction_fixture.owner,
        interaction_fixture.conversation.id,
        interaction_fixture.turn.id,
        status="completed",
    )

    assert (
        await service.get_active_turn_snapshot(
            interaction_fixture.db, subject, interaction_fixture.conversation.id
        )
        is None
    )
    terminal = await service.get_turn_snapshot(
        interaction_fixture.db,
        subject,
        interaction_fixture.conversation.id,
        interaction_fixture.turn.id,
    )
    assert terminal.status == "completed"
    assert terminal.version == 2
    assert [(item.status, item.version) for item in terminal.questions] == [
        ("answered", 2)
    ]

    with pytest.raises(InteractionNotFound):
        await service.get_turn_snapshot(
            interaction_fixture.db,
            AuthorizationSubject(uuid4(), interaction_fixture.org, "org_admin"),
            interaction_fixture.conversation.id,
            interaction_fixture.turn.id,
        )


async def test_cancel_active_turn_is_idempotent_for_conversation_delete(
    interaction_fixture,
) -> None:
    service, *_ = _service()
    await _execution(interaction_fixture, service)
    owner = AuthorizationSubject(interaction_fixture.owner, interaction_fixture.org)

    first = await service.cancel_active_turn(
        interaction_fixture.db,
        owner,
        interaction_fixture.conversation.id,
    )
    second = await service.cancel_active_turn(
        interaction_fixture.db,
        owner,
        interaction_fixture.conversation.id,
    )

    assert first is not None
    cancelled_turn_id, targets = first
    assert cancelled_turn_id == interaction_fixture.turn.id
    assert len(targets) == 1
    assert second is None
    assert interaction_fixture.turn.status == "cancelled"


async def test_cancel_signal_always_addresses_guarded_conversation_root() -> None:
    from src.services.interaction import ExecutionTarget, InteractionService

    conversation_id, turn_id, child_run_id = uuid4(), uuid4(), uuid4()
    root_handle = AsyncMock()
    child_handle = AsyncMock()
    client = MagicMock()

    def handle(workflow_id, *, run_id=None):  # noqa: ANN001
        if workflow_id == f"conversation-{conversation_id}":
            assert run_id is None
            return root_handle
        assert workflow_id == "child-workflow"
        assert run_id == str(child_run_id)
        return child_handle

    client.get_workflow_handle.side_effect = handle
    with patch("src.services.interaction.get_temporal_client", return_value=client):
        await InteractionService.signal_turn_cancel(
            [ExecutionTarget("child-workflow", child_run_id)],
            turn_id,
            conversation_id,
        )

    root_handle.signal.assert_awaited_once_with("cancel_turn", str(turn_id))
    child_handle.signal.assert_awaited_once_with("cancel_turn", str(turn_id))


def _payload_hash(request: QuestionRequest) -> str:
    return hashlib.sha256(
        json.dumps(
            request.model_dump(by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


async def test_finish_turn_is_exact_and_idempotent(interaction_fixture) -> None:
    service, *_ = _service()
    await service.finish_turn(
        interaction_fixture.db,
        interaction_fixture.org,
        interaction_fixture.owner,
        interaction_fixture.conversation.id,
        interaction_fixture.turn.id,
        status="completed",
    )
    await service.finish_turn(
        interaction_fixture.db,
        interaction_fixture.org,
        interaction_fixture.owner,
        interaction_fixture.conversation.id,
        interaction_fixture.turn.id,
        status="completed",
    )
    assert interaction_fixture.turn.status == "completed"
    assert interaction_fixture.turn.terminal_at is not None


async def test_failed_turn_cancels_pending_question_without_erasing_answer(
    interaction_fixture,
) -> None:
    service, *_ = _service()
    pending = await _question(interaction_fixture, service)
    await service.finish_turn(
        interaction_fixture.db,
        interaction_fixture.org,
        interaction_fixture.owner,
        interaction_fixture.conversation.id,
        interaction_fixture.turn.id,
        status="failed",
    )
    saved = await interaction_fixture.db.get(UserQuestion, pending.question_id)
    assert interaction_fixture.turn.status == "failed"
    assert saved is not None and saved.status == "cancelled"


async def test_begin_turn_rejects_a_second_active_turn(interaction_fixture) -> None:
    from src.services.interaction import InteractionConflict

    service, *_ = _service()
    subject = AuthorizationSubject(interaction_fixture.owner, interaction_fixture.org)
    assert (
        await service.begin_turn(
            interaction_fixture.db,
            subject,
            interaction_fixture.conversation.id,
            interaction_fixture.turn.id,
            None,
        )
    ).id == interaction_fixture.turn.id
    with pytest.raises(InteractionConflict):
        await service.begin_turn(
            interaction_fixture.db,
            subject,
            interaction_fixture.conversation.id,
            uuid4(),
            None,
        )


class _Converter:
    def __init__(self, decoded):  # noqa: ANN001
        self.decoded = decoded

    async def decode(self, payloads):  # noqa: ANN001
        return [self.decoded]


class _TemporalHandle:
    def __init__(self, description, start):  # noqa: ANN001
        self.description = description
        self.start = start

    async def describe(self):
        return self.description

    async def fetch_history(self):
        return SimpleNamespace(
            events=[SimpleNamespace(workflow_execution_started_event_attributes=self.start)]
        )


class _TemporalClient:
    def __init__(self, description, start, decoded):  # noqa: ANN001
        self.handle = _TemporalHandle(description, start)
        self.data_converter = _Converter(decoded)
        self.calls = []

    def get_workflow_handle(self, workflow_id, *, run_id):  # noqa: ANN001
        self.calls.append((workflow_id, run_id))
        return self.handle


class _RoutedTemporalClient:
    def __init__(self, handles, decoded):  # noqa: ANN001
        self.handles = handles
        self.decoded = decoded
        self.calls = []
        self.data_converter = self

    def get_workflow_handle(self, workflow_id, *, run_id):  # noqa: ANN001
        self.calls.append((workflow_id, run_id))
        return self.handles[(workflow_id, run_id)]

    async def decode(self, payloads):  # noqa: ANN001
        return [self.decoded[payloads[0]]]


def _start(*, parent_id="", parent_run=""):  # noqa: ANN001
    return SimpleNamespace(
        input=SimpleNamespace(payloads=[object()]),
        parent_workflow_execution=SimpleNamespace(workflow_id=parent_id, run_id=parent_run),
    )


async def test_temporal_verifier_accepts_exact_ordinary_root() -> None:
    from src.services.interaction import TemporalExecutionVerifier

    conversation_id, run_id = uuid4(), uuid4()
    workflow_id = f"conversation-{conversation_id}"
    description = SimpleNamespace(
        id=workflow_id,
        run_id=str(run_id),
        workflow_type="ConversationWorkflow",
        task_queue=f"session-{conversation_id}",
    )
    client = _TemporalClient(description, _start(), {"conversation_id": str(conversation_id)})
    verifier = TemporalExecutionVerifier(client)
    turn = SimpleNamespace(conversation_id=conversation_id, workflow_run_id=None)
    await verifier.verify(
        turn, _registration(conversation_id).model_copy(update={"run_id": run_id})
    )
    assert client.calls == [(workflow_id, str(run_id))]


@pytest.mark.parametrize("field", ["run_id", "task_queue", "input", "parent"])
async def test_temporal_verifier_rejects_mismatched_runtime_identity(field) -> None:
    from src.services.interaction import InteractionNotFound, TemporalExecutionVerifier

    conversation_id, run_id = uuid4(), uuid4()
    workflow_id = f"conversation-{conversation_id}"
    description = SimpleNamespace(
        id=workflow_id,
        run_id=str(uuid4()) if field == "run_id" else str(run_id),
        workflow_type="ConversationWorkflow",
        task_queue="wrong" if field == "task_queue" else f"session-{conversation_id}",
    )
    decoded = {"conversation_id": str(uuid4() if field == "input" else conversation_id)}
    start = _start(parent_id="foreign" if field == "parent" else "")
    verifier = TemporalExecutionVerifier(_TemporalClient(description, start, decoded))
    turn = SimpleNamespace(conversation_id=conversation_id, workflow_run_id=None)
    with pytest.raises(InteractionNotFound):
        await verifier.verify(
            turn, _registration(conversation_id).model_copy(update={"run_id": run_id})
        )


@pytest.mark.parametrize("wrong_agent", [False, True])
async def test_temporal_verifier_checks_exact_child_parent_and_agent(wrong_agent) -> None:
    from src.services.interaction import InteractionNotFound, TemporalExecutionVerifier

    workflow_run_id, child_run_id, parent_run_id, agent_id, turn_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    child_id = f"{workflow_run_id}:node:research"
    parent_id = f"{workflow_run_id}:orchestrator"
    child_payload, parent_payload = object(), object()
    child_start = SimpleNamespace(
        input=SimpleNamespace(payloads=[child_payload]),
        parent_workflow_execution=SimpleNamespace(workflow_id=parent_id, run_id=str(parent_run_id)),
    )
    parent_start = SimpleNamespace(
        input=SimpleNamespace(payloads=[parent_payload]),
        parent_workflow_execution=SimpleNamespace(workflow_id="root", run_id=str(uuid4())),
    )
    queue = "session-conversation"
    handles = {
        (child_id, str(child_run_id)): _TemporalHandle(
            SimpleNamespace(
                id=child_id,
                run_id=str(child_run_id),
                workflow_type="AgentWorkflow",
                task_queue=queue,
            ),
            child_start,
        ),
        (parent_id, str(parent_run_id)): _TemporalHandle(
            SimpleNamespace(
                id=parent_id,
                run_id=str(parent_run_id),
                workflow_type="MultiAgentWorkflow",
                task_queue=queue,
            ),
            parent_start,
        ),
    }
    client = _RoutedTemporalClient(
        handles,
        {
            child_payload: {
                "workflow_run_id": str(workflow_run_id),
                "workflow_phase": "node",
                "workflow_node_key": "research",
                "turn_id": str(turn_id),
                "execution_iteration": 1,
                "agent": {"agent_definition_id": str(uuid4() if wrong_agent else agent_id)},
            },
            parent_payload: {"run_id": str(workflow_run_id)},
        },
    )
    data = ExecutionRegistrationRequest(
        workflow_id=child_id,
        run_id=child_run_id,
        task_queue=queue,
        phase="node",
        node_key="research",
        agent_definition_id=agent_id,
    )
    verifier = TemporalExecutionVerifier(client)
    turn = SimpleNamespace(
        id=turn_id,
        conversation_id=uuid4(),
        workflow_run_id=workflow_run_id,
    )
    if wrong_agent:
        with pytest.raises(InteractionNotFound):
            await verifier.verify(turn, data)
    else:
        await verifier.verify(turn, data)
        assert client.calls == [(child_id, str(child_run_id)), (parent_id, str(parent_run_id))]


@pytest.mark.parametrize("mismatch", ["turn_id", "execution_iteration"])
async def test_temporal_verifier_rejects_forged_child_turn_or_iteration(mismatch) -> None:
    from src.services.interaction import InteractionNotFound, TemporalExecutionVerifier

    workflow_run_id, child_run_id, parent_run_id, agent_id, turn_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    child_id = f"{workflow_run_id}:node:research"
    parent_id = f"{workflow_run_id}:orchestrator"
    queue = "session-conversation"
    handles = {
        (child_id, str(child_run_id)): _TemporalHandle(
            SimpleNamespace(
                id=child_id,
                run_id=str(child_run_id),
                workflow_type="AgentWorkflow",
                task_queue=queue,
            ),
            _start(parent_id=parent_id, parent_run=str(parent_run_id)),
        ),
        (parent_id, str(parent_run_id)): _TemporalHandle(
            SimpleNamespace(
                id=parent_id,
                run_id=str(parent_run_id),
                workflow_type="MultiAgentWorkflow",
                task_queue=queue,
            ),
            _start(parent_id="root", parent_run=str(uuid4())),
        ),
    }
    client = _RoutedTemporalClient(
        handles,
        {
            handles[(child_id, str(child_run_id))].start.input.payloads[0]: {
                "workflow_run_id": str(workflow_run_id),
                "workflow_phase": "node",
                "workflow_node_key": "research",
                "agent": {"agent_definition_id": str(agent_id)},
                "turn_id": str(uuid4() if mismatch == "turn_id" else turn_id),
                "execution_iteration": 2 if mismatch == "execution_iteration" else 1,
            },
            handles[(parent_id, str(parent_run_id))].start.input.payloads[0]: {
                "run_id": str(workflow_run_id)
            },
        },
    )
    data = ExecutionRegistrationRequest(
        workflow_id=child_id,
        run_id=child_run_id,
        task_queue=queue,
        phase="node",
        node_key="research",
        agent_definition_id=agent_id,
        iteration=1,
    )
    turn = SimpleNamespace(
        id=turn_id,
        conversation_id=uuid4(),
        workflow_run_id=workflow_run_id,
    )

    with pytest.raises(InteractionNotFound):
        await TemporalExecutionVerifier(client).verify(turn, data)
