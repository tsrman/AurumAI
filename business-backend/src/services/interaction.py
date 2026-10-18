"""Owner-bound interaction use cases with live authorization and exact runtime identity."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.authorization_types import AuthorizationSubject
from src.core.capability_catalog import Capability
from src.core.temporal import get_temporal_client
from src.models.agent_definition import AgentDefinition
from src.models.conversation import Conversation
from src.models.data_identity import ConversationIdentity
from src.models.interaction import InteractionExecution, InteractionTurn, UserQuestion
from src.models.workflow_run import WorkflowRun
from src.repositories.interaction_repository import InteractionError, InteractionRepository
from src.schemas.interaction import (
    AnswerRequest,
    ExecutionRegistrationRequest,
    QuestionCreateRequest,
    QuestionResponse,
    TurnQuestionState,
    TurnLimits,
    TurnSnapshot,
)
from src.services.authorization import (
    AuthorizationService,
    CapabilityAccessDenied,
    authorization_service,
)
from src.services.model_access import ensure_model_allowed
from src.services.workflow_authorization import (
    WorkflowAuthorizationService,
    WorkflowStepAccessDenied,
    workflow_authorization_service,
)


class InteractionNotFound(Exception):
    pass


class InteractionAccessDenied(Exception):
    pass


class InteractionConflict(Exception):
    def __init__(self, code: str = "interaction_conflict") -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ExecutionTarget:
    workflow_id: str
    run_id: UUID


class ExecutionVerifier(Protocol):
    async def verify(self, turn: InteractionTurn, data: ExecutionRegistrationRequest) -> None: ...


ModelAuthorizer = Callable[..., Awaitable[None]]


class TemporalExecutionVerifier:
    """Fail-closed projection of immutable Temporal execution identity."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _description(self, workflow_id: str, run_id: str) -> tuple[Any, Any]:
        try:
            handle = self._client.get_workflow_handle(workflow_id, run_id=run_id)
            description = await handle.describe()
            history = await handle.fetch_history()
            start = history.events[0].workflow_execution_started_event_attributes
        except Exception as exc:
            raise InteractionNotFound() from exc
        if description.id != workflow_id or str(description.run_id) != run_id:
            raise InteractionNotFound()
        return description, start

    async def _input(self, start: Any) -> dict[str, Any]:
        try:
            decoded = await self._client.data_converter.decode(start.input.payloads)
        except Exception as exc:
            raise InteractionNotFound() from exc
        if len(decoded) != 1:
            raise InteractionNotFound()
        value = decoded[0]
        if not isinstance(value, dict):
            try:
                value = vars(value)
            except TypeError as exc:
                raise InteractionNotFound() from exc
        return cast(dict[str, Any], value)

    async def verify(self, turn: InteractionTurn, data: ExecutionRegistrationRequest) -> None:
        description, start = await self._description(data.workflow_id, str(data.run_id))
        if description.task_queue != data.task_queue:
            raise InteractionNotFound()
        payload = await self._input(start)
        parent = start.parent_workflow_execution
        parent_id = str(getattr(parent, "workflow_id", ""))
        parent_run_id = str(getattr(parent, "run_id", ""))

        if data.phase == "ordinary":
            if (
                turn.workflow_run_id is not None
                or description.workflow_type != "ConversationWorkflow"
                or data.workflow_id != f"conversation-{turn.conversation_id}"
                or data.task_queue != f"session-{turn.conversation_id}"
                or payload.get("conversation_id") != str(turn.conversation_id)
                or parent_id
                or parent_run_id
            ):
                raise InteractionNotFound()
            return

        workflow_run_id = turn.workflow_run_id
        if workflow_run_id is None:
            raise InteractionNotFound()
        expected_id = (
            f"{workflow_run_id}:node:{data.node_key}"
            if data.phase == "node"
            else f"{workflow_run_id}:{data.phase}"
        )
        expected_parent = f"{workflow_run_id}:orchestrator"
        raw_agent = payload.get("agent")
        registered_agent = (
            raw_agent.get("agent_definition_id")
            if isinstance(raw_agent, dict)
            else getattr(raw_agent, "agent_definition_id", None)
        )
        if (
            description.workflow_type != "AgentWorkflow"
            or data.workflow_id != expected_id
            or payload.get("workflow_run_id") != str(workflow_run_id)
            or payload.get("workflow_phase") != data.phase
            or payload.get("workflow_node_key") != data.node_key
            or payload.get("turn_id") != str(turn.id)
            or payload.get("execution_iteration", 1) != data.iteration
            or registered_agent != str(data.agent_definition_id)
            or parent_id != expected_parent
            or not parent_run_id
        ):
            raise InteractionNotFound()
        parent_description, parent_start = await self._description(expected_parent, parent_run_id)
        parent_payload = await self._input(parent_start)
        if (
            parent_description.workflow_type != "MultiAgentWorkflow"
            or parent_description.task_queue != data.task_queue
            or parent_payload.get("run_id") != str(workflow_run_id)
        ):
            raise InteractionNotFound()


class InteractionService:
    def __init__(
        self,
        *,
        authorization: AuthorizationService = authorization_service,
        model_authorizer: ModelAuthorizer = ensure_model_allowed,
        workflow_authorization: WorkflowAuthorizationService = workflow_authorization_service,
        execution_verifier: ExecutionVerifier | None = None,
    ) -> None:
        self._authorization = authorization
        self._model_authorizer = model_authorizer
        self._workflow_authorization = workflow_authorization
        self._execution_verifier = execution_verifier

    @staticmethod
    def _repo(subject: AuthorizationSubject) -> InteractionRepository:
        return InteractionRepository(subject.org_id)

    @staticmethod
    async def _turn(
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
        turn_id: UUID,
        *,
        lock: bool = False,
    ) -> InteractionTurn:
        if lock:
            from src.services.conversation_lifecycle import lock_active_conversation
            await lock_active_conversation(session, subject.org_id, conversation_id)
        statement = select(InteractionTurn).where(
            InteractionTurn.conversation_id.in_(select(ConversationIdentity.id).where(ConversationIdentity.lifecycle == "active")),
            InteractionTurn.id == turn_id,
            InteractionTurn.org_id == subject.org_id,
            InteractionTurn.owner_id == subject.user_id,
            InteractionTurn.conversation_id == conversation_id,
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        turn = await session.scalar(statement)
        if turn is None:
            raise InteractionNotFound()
        return turn

    @staticmethod
    async def _context(
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
        *,
        execution_id: UUID | None = None,
        question_id: UUID | None = None,
        lock_turn: bool = False,
    ) -> tuple[InteractionTurn, InteractionExecution, UserQuestion | None]:
        if lock_turn:
            from src.services.conversation_lifecycle import lock_active_conversation
            await lock_active_conversation(session, subject.org_id, conversation_id)
        statement = (
            select(InteractionTurn, InteractionExecution, UserQuestion)
            .join(
                InteractionExecution,
                and_(
                    InteractionExecution.org_id == InteractionTurn.org_id,
                    InteractionExecution.turn_id == InteractionTurn.id,
                ),
            )
            .outerjoin(
                UserQuestion,
                and_(
                    UserQuestion.org_id == InteractionExecution.org_id,
                    UserQuestion.execution_id == InteractionExecution.id,
                ),
            )
            .where(
                InteractionTurn.org_id == subject.org_id,
                InteractionTurn.owner_id == subject.user_id,
                InteractionTurn.conversation_id == conversation_id,
                InteractionTurn.conversation_id.in_(select(ConversationIdentity.id).where(ConversationIdentity.lifecycle == "active")),
            )
        )
        if execution_id is not None:
            statement = statement.where(InteractionExecution.id == execution_id)
        if question_id is not None:
            statement = statement.where(UserQuestion.id == question_id)
        if lock_turn:
            statement = statement.with_for_update(of=InteractionTurn).execution_options(
                populate_existing=True
            )
        row = (await session.execute(statement)).first()
        if row is None or (question_id is not None and row.UserQuestion is None):
            raise InteractionNotFound()
        return row.InteractionTurn, row.InteractionExecution, row.UserQuestion

    async def _require_live(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        turn: InteractionTurn,
        execution: InteractionExecution | None = None,
    ) -> None:
        try:
            await self._authorization.require_capability(session, subject, Capability.CHAT_SEND)
            if turn.workflow_run_id is None:
                conversation = await session.scalar(
                    select(Conversation).where(
                        Conversation.id == turn.conversation_id,
                        Conversation.org_id == subject.org_id,
                        Conversation.user_id == subject.user_id,
                    )
                )
                if conversation is None:
                    raise InteractionNotFound()
                await self._model_authorizer(
                    session,
                    str(subject.org_id),
                    conversation.model,
                    user_id=str(subject.user_id),
                    role=subject.role,
                )
                return
            if execution is None:
                raise InteractionAccessDenied()
            run = await session.scalar(
                select(WorkflowRun).where(
                    WorkflowRun.id == turn.workflow_run_id,
                    WorkflowRun.org_id == subject.org_id,
                    WorkflowRun.initiating_user_id == subject.user_id,
                    WorkflowRun.conversation_id == turn.conversation_id,
                    WorkflowRun.turn_id == turn.id,
                )
            )
            if run is None:
                raise InteractionNotFound()
            await self._workflow_authorization.require_snapshot_step_executable(
                session,
                subject,
                run,
                phase=execution.phase,
                node_key=execution.node_key,
            )
        except InteractionNotFound:
            raise
        except (CapabilityAccessDenied, WorkflowStepAccessDenied, HTTPException) as exc:
            raise InteractionAccessDenied() from exc

    @staticmethod
    def _expected_agent(run: WorkflowRun, data: ExecutionRegistrationRequest) -> UUID:
        snapshot = run.execution_snapshot
        if data.phase == "node":
            nodes = snapshot.get("nodes")
            if not isinstance(nodes, list):
                raise InteractionNotFound()
            matches = [
                item
                for item in nodes
                if isinstance(item, dict) and item.get("nodeKey") == data.node_key
            ]
            if len(matches) != 1 or not isinstance(matches[0].get("agent"), dict):
                raise InteractionNotFound()
            raw = matches[0]["agent"].get("agentDefinitionId")
        else:
            supervisor = snapshot.get("supervisor")
            if not isinstance(supervisor, dict):
                raise InteractionNotFound()
            raw = supervisor.get("agentDefinitionId")
        try:
            return UUID(str(raw))
        except ValueError as exc:
            raise InteractionNotFound() from exc

    async def begin_turn(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
        turn_id: UUID,
        workflow_run_id: UUID | None,
    ) -> InteractionTurn:
        conversation = await session.scalar(
            select(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.id.in_(select(ConversationIdentity.id).where(ConversationIdentity.lifecycle == "active")),
                Conversation.org_id == subject.org_id,
                Conversation.user_id == subject.user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if conversation is None:
            raise InteractionNotFound()
        existing = await session.scalar(
            select(InteractionTurn)
            .where(
                InteractionTurn.org_id == subject.org_id,
                InteractionTurn.conversation_id == conversation_id,
                InteractionTurn.conversation_id.in_(select(ConversationIdentity.id).where(ConversationIdentity.lifecycle == "active")),
                InteractionTurn.status == "active",
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.id == turn_id and existing.workflow_run_id == workflow_run_id:
                return existing
            raise InteractionConflict()
        turn = InteractionTurn(
            id=turn_id,
            org_id=subject.org_id,
            conversation_id=conversation_id,
            owner_id=subject.user_id,
            workflow_run_id=workflow_run_id,
            limits=TurnLimits().model_dump(),
        )
        session.add(turn)
        await session.flush()
        return turn

    async def register_execution(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
        turn_id: UUID,
        data: ExecutionRegistrationRequest,
    ) -> InteractionExecution:
        turn = await self._turn(session, subject, conversation_id, turn_id)
        if turn.status != "active":
            raise InteractionConflict()
        provisional = InteractionExecution(
            org_id=subject.org_id,
            turn_id=turn.id,
            workflow_id=data.workflow_id,
            run_id=data.run_id,
            task_queue=data.task_queue,
            phase=data.phase,
            node_key=data.node_key,
            agent_definition_id=data.agent_definition_id,
            iteration=data.iteration,
        )
        await self._require_live(session, subject, turn, provisional)
        if data.phase != "ordinary":
            run = await session.get(WorkflowRun, turn.workflow_run_id)
            if run is None or data.agent_definition_id != self._expected_agent(run, data):
                raise InteractionNotFound()
        verifier = self._execution_verifier or TemporalExecutionVerifier(get_temporal_client())
        await verifier.verify(turn, data)
        turn = await self._turn(session, subject, conversation_id, turn_id, lock=True)
        if turn.status != "active":
            raise InteractionConflict()
        existing = await session.scalar(
            select(InteractionExecution).where(
                InteractionExecution.org_id == subject.org_id,
                InteractionExecution.turn_id == turn.id,
                InteractionExecution.workflow_id == data.workflow_id,
                InteractionExecution.run_id == data.run_id,
            )
        )
        if existing is not None:
            comparable = (
                existing.task_queue,
                existing.phase,
                existing.node_key,
                existing.agent_definition_id,
                existing.iteration,
            )
            wanted = (
                data.task_queue,
                data.phase,
                data.node_key,
                data.agent_definition_id,
                data.iteration,
            )
            if comparable != wanted:
                raise InteractionConflict()
            return existing
        session.add(provisional)
        await session.flush()
        return provisional

    async def create_question(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
        execution_id: UUID,
        data: QuestionCreateRequest,
        *,
        allow_new: bool = True,
    ) -> QuestionResponse:
        turn, execution, _ = await self._context(
            session,
            subject,
            conversation_id,
            execution_id=execution_id,
            lock_turn=True,
        )
        await self._require_live(session, subject, turn, execution)
        repository = self._repo(subject)
        try:
            operation = await repository.reserve_operation(
                session,
                execution.id,
                data.invocation_id,
                "ask_user",
                self._question_hash(data),
                allow_new=allow_new,
            )
            question = await repository.create_question(session, operation.id, data.question)
        except InteractionError as exc:
            raise self._repository_error(exc) from exc
        return await self._response(session, question)

    async def authorize_web_operation(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
        turn_id: UUID,
        execution_id: UUID,
    ) -> None:
        """Authorize the exact live runtime target before any web reservation/I/O."""
        turn, execution, _ = await self._context(
            session,
            subject,
            conversation_id,
            execution_id=execution_id,
            lock_turn=True,
        )
        if turn.id != turn_id:
            raise InteractionNotFound()
        if turn.status != "active":
            # Same vocabulary the question path uses (interaction_repository's
            # question conflict), so callers can tell a user cancellation apart
            # from any other reason the turn stopped being live.
            raise InteractionConflict(
                "user_input_cancelled" if turn.status == "cancelled" else "interaction_conflict"
            )
        await self._require_live(session, subject, turn, execution)
        try:
            await self._authorization.require_capability(
                session,
                subject,
                Capability.WEB_ACCESS,
            )
        except CapabilityAccessDenied as exc:
            raise InteractionAccessDenied() from exc

    @staticmethod
    def _question_hash(data: QuestionCreateRequest) -> str:
        from src.services.interaction_rules import question_arguments_hash

        return question_arguments_hash(data.question)

    async def get_question(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
        question_id: UUID,
    ) -> QuestionResponse:
        _, _, question = await self._context(
            session, subject, conversation_id, question_id=question_id
        )
        assert question is not None
        return await self._response(session, question)

    async def list_questions(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
    ) -> list[QuestionResponse]:
        conversation = await session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.id.in_(select(ConversationIdentity.id).where(ConversationIdentity.lifecycle == "active")),
                Conversation.org_id == subject.org_id,
                Conversation.user_id == subject.user_id,
            )
        )
        if conversation is None:
            raise InteractionNotFound()
        questions = list(
            (
                await session.scalars(
                    select(UserQuestion)
                    .join(
                        InteractionTurn,
                        and_(
                            InteractionTurn.org_id == UserQuestion.org_id,
                            InteractionTurn.id == UserQuestion.turn_id,
                        ),
                    )
                    .where(
                        UserQuestion.org_id == subject.org_id,
                        InteractionTurn.conversation_id == conversation_id,
                InteractionTurn.conversation_id.in_(select(ConversationIdentity.id).where(ConversationIdentity.lifecycle == "active")),
                        InteractionTurn.owner_id == subject.user_id,
                    )
                    .order_by(UserQuestion.created_at, UserQuestion.id)
                )
            ).all()
        )
        return [await self._response(session, question) for question in questions]

    async def get_active_turn_snapshot(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
    ) -> TurnSnapshot | None:
        return await self._turn_snapshot(
            session, subject, conversation_id, turn_id=None, active_only=True
        )

    async def get_turn_snapshot(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> TurnSnapshot:
        snapshot = await self._turn_snapshot(
            session, subject, conversation_id, turn_id=turn_id, active_only=False
        )
        if snapshot is None:  # exact lookups never return None
            raise InteractionNotFound()
        return snapshot

    @staticmethod
    async def _turn_snapshot(
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
        *,
        turn_id: UUID | None,
        active_only: bool,
    ) -> TurnSnapshot | None:
        conversation = await session.scalar(
            select(Conversation.id).where(
                Conversation.id == conversation_id,
                Conversation.id.in_(select(ConversationIdentity.id).where(ConversationIdentity.lifecycle == "active")),
                Conversation.org_id == subject.org_id,
                Conversation.user_id == subject.user_id,
            )
        )
        if conversation is None:
            raise InteractionNotFound()
        statement = select(InteractionTurn).where(
            InteractionTurn.org_id == subject.org_id,
            InteractionTurn.conversation_id == conversation_id,
            InteractionTurn.owner_id == subject.user_id,
        )
        if turn_id is not None:
            statement = statement.where(InteractionTurn.id == turn_id)
        if active_only:
            statement = statement.where(InteractionTurn.status == "active")
        turn = await session.scalar(statement.order_by(InteractionTurn.created_at.desc()))
        if turn is None:
            if active_only:
                return None
            raise InteractionNotFound()
        question_rows = (
            await session.execute(
                select(UserQuestion.id, UserQuestion.status, UserQuestion.version)
                .where(
                    UserQuestion.org_id == subject.org_id,
                    UserQuestion.turn_id == turn.id,
                )
                .order_by(UserQuestion.created_at, UserQuestion.id)
            )
        ).all()
        return TurnSnapshot(
            turn_id=turn.id,
            status=cast(Any, turn.status),
            version=turn.version,
            questions=[
                TurnQuestionState(
                    question_id=question_id,
                    status=cast(Any, question_status),
                    version=question_version,
                )
                for question_id, question_status, question_version in question_rows
            ],
        )

    async def answer_question(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
        question_id: UUID,
        key: UUID,
        answer: AnswerRequest,
    ) -> QuestionResponse:
        turn, execution, _ = await self._context(
            session,
            subject,
            conversation_id,
            question_id=question_id,
            lock_turn=True,
        )
        await self._require_live(session, subject, turn, execution)
        try:
            question = await self._repo(subject).answer_question(
                session, question_id, subject.user_id, key, answer
            )
        except InteractionError as exc:
            raise self._repository_error(exc) from exc
        return await self._response(session, question)

    async def consume_question(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
        question_id: UUID,
    ) -> AnswerRequest:
        turn, execution, _ = await self._context(
            session,
            subject,
            conversation_id,
            question_id=question_id,
            lock_turn=True,
        )
        await self._require_live(session, subject, turn, execution)
        try:
            question = await self._repo(subject).consume_question(session, question_id)
        except InteractionError as exc:
            raise self._repository_error(exc) from exc
        return AnswerRequest.model_validate(question.answer)

    async def expire_question(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
        question_id: UUID,
    ) -> QuestionResponse:
        await self._context(session, subject, conversation_id, question_id=question_id)
        try:
            question = await self._repo(subject).expire_question(session, question_id)
        except InteractionError as exc:
            raise self._repository_error(exc) from exc
        return await self._response(session, question)

    async def cancel_turn(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
        turn_id: UUID,
    ) -> list[ExecutionTarget]:
        turn = await self._turn(session, subject, conversation_id, turn_id)
        targets = [
            ExecutionTarget(row.workflow_id, row.run_id)
            for row in (
                await session.scalars(
                    select(InteractionExecution)
                    .where(
                        InteractionExecution.org_id == subject.org_id,
                        InteractionExecution.turn_id == turn.id,
                    )
                    .order_by(InteractionExecution.id)
                )
            ).all()
        ]
        try:
            await self._repo(subject).cancel_turn(session, turn.id)
        except InteractionError as exc:
            raise self._repository_error(exc) from exc
        return targets

    async def cancel_active_turn(
        self,
        session: AsyncSession,
        subject: AuthorizationSubject,
        conversation_id: UUID,
    ) -> tuple[UUID, list[ExecutionTarget]] | None:
        conversation = await session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.id.in_(select(ConversationIdentity.id).where(ConversationIdentity.lifecycle == "active")),
                Conversation.org_id == subject.org_id,
                Conversation.user_id == subject.user_id,
            )
        )
        if conversation is None:
            raise InteractionNotFound()
        turn = await session.scalar(
            select(InteractionTurn)
            .where(
                InteractionTurn.org_id == subject.org_id,
                InteractionTurn.conversation_id == conversation_id,
                InteractionTurn.conversation_id.in_(select(ConversationIdentity.id).where(ConversationIdentity.lifecycle == "active")),
                InteractionTurn.owner_id == subject.user_id,
                InteractionTurn.status == "active",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if turn is None:
            return None
        targets = [
            ExecutionTarget(row.workflow_id, row.run_id)
            for row in (
                await session.scalars(
                    select(InteractionExecution)
                    .where(
                        InteractionExecution.org_id == subject.org_id,
                        InteractionExecution.turn_id == turn.id,
                    )
                    .order_by(InteractionExecution.id)
                )
            ).all()
        ]
        try:
            await self._repo(subject).cancel_turn(session, turn.id)
        except InteractionError as exc:
            raise self._repository_error(exc) from exc
        return turn.id, targets

    @staticmethod
    async def signal_turn_cancel(
        targets: list[ExecutionTarget],
        turn_id: UUID,
        conversation_id: UUID,
    ) -> None:
        client = get_temporal_client()
        root_workflow_id = f"conversation-{conversation_id}"
        root_target = next(
            (target for target in targets if target.workflow_id == root_workflow_id),
            None,
        )
        root_handle = client.get_workflow_handle(
            root_workflow_id,
            run_id=str(root_target.run_id) if root_target is not None else None,
        )
        await root_handle.signal("cancel_turn", str(turn_id))
        for target in targets:
            if target is root_target:
                continue
            handle = client.get_workflow_handle(target.workflow_id, run_id=str(target.run_id))
            await handle.signal("cancel_turn", str(turn_id))

    async def finish_turn(
        self,
        session: AsyncSession,
        org_id: UUID,
        owner_id: UUID,
        conversation_id: UUID,
        turn_id: UUID,
        *,
        status: str,
    ) -> None:
        await self._turn(
            session,
            AuthorizationSubject(owner_id, org_id),
            conversation_id,
            turn_id,
        )
        try:
            await InteractionRepository(org_id).finish_turn(session, turn_id, status)
        except InteractionError as exc:
            raise self._repository_error(exc) from exc

    async def _response(self, session: AsyncSession, question: UserQuestion) -> QuestionResponse:
        execution = await session.get(InteractionExecution, question.execution_id)
        if execution is None:
            raise InteractionNotFound()
        agent_name = None
        if execution.agent_definition_id is not None:
            agent_name = await session.scalar(
                select(AgentDefinition.name).where(
                    AgentDefinition.id == execution.agent_definition_id,
                    AgentDefinition.org_id == question.org_id,
                )
            )
        return QuestionResponse.model_validate(
            {
                "question_id": question.id,
                "turn_id": question.turn_id,
                "text": question.text,
                "options": question.options,
                "status": question.status,
                "version": question.version,
                "expires_at": question.expires_at,
                "created_at": question.created_at,
                "phase": execution.phase,
                "node_key": execution.node_key,
                "agent_name": agent_name,
                "answer": question.answer,
                "answered_at": question.answered_at,
            }
        )

    @staticmethod
    def _repository_error(exc: InteractionError) -> Exception:
        if exc.code == "interaction_not_found":
            return InteractionNotFound()
        if exc.code in {
            "user_input_disabled",
            "user_input_limit",
            "interaction_conflict",
        }:
            return InteractionConflict(exc.code)
        if exc.code == "authorization_revoked":
            return InteractionAccessDenied()
        return InteractionConflict()


interaction_service = InteractionService()
