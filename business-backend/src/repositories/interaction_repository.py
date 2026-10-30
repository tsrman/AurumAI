"""Org-scoped durable mutations. The caller owns commit/rollback and live authorization.

All writers serialize on the turn row BEFORE execution/operation/question locks.
Do not commit a failed reserve/create pair: use one caller-owned transaction.
No network IO, Temporal signal or permission snapshot belongs in this repository.
"""

import re
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.interaction import (
    InteractionExecution,
    InteractionOutbox,
    InteractionTurn,
    ToolOperation,
    UserQuestion,
)
from src.schemas.interaction import AnswerRequest, QuestionRequest, TurnLimits
from src.services.conversation_files import MAX_FILES_PER_TURN
from src.services.interaction_rules import answer_allowed, question_arguments_hash


class InteractionError(ValueError):
    """Stable, payload-free error for the service/API layer."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class InteractionRepository:
    def __init__(self, org_id: UUID) -> None:
        self.org_id = org_id

    async def _turn(self, session: AsyncSession, turn_id: UUID) -> InteractionTurn:
        from src.services.conversation_lifecycle import lock_active_conversation
        conversation_id = await session.scalar(select(InteractionTurn.conversation_id).where(InteractionTurn.id == turn_id, InteractionTurn.org_id == self.org_id))
        if conversation_id is not None:
            await lock_active_conversation(session, self.org_id, conversation_id)
        row = await session.scalar(
            select(InteractionTurn)
            .where(
                InteractionTurn.org_id == self.org_id,
                InteractionTurn.id == turn_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise InteractionError("interaction_not_found")
        return row

    async def _execution(
        self,
        session: AsyncSession,
        turn_id: UUID,
        execution_id: UUID,
    ) -> InteractionExecution:
        row = await session.scalar(
            select(InteractionExecution)
            .where(
                InteractionExecution.org_id == self.org_id,
                InteractionExecution.turn_id == turn_id,
                InteractionExecution.id == execution_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise InteractionError("interaction_not_found")
        return row

    async def _operation(
        self,
        session: AsyncSession,
        turn_id: UUID,
        execution_id: UUID,
        operation_id: UUID,
    ) -> ToolOperation:
        row = await session.scalar(
            select(ToolOperation)
            .where(
                ToolOperation.org_id == self.org_id,
                ToolOperation.turn_id == turn_id,
                ToolOperation.execution_id == execution_id,
                ToolOperation.id == operation_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise InteractionError("interaction_not_found")
        return row

    async def _question_context(
        self,
        session: AsyncSession,
        question_id: UUID,
    ) -> tuple[InteractionTurn, ToolOperation, UserQuestion]:
        identity = (
            await session.execute(
                select(
                    UserQuestion.turn_id,
                    UserQuestion.execution_id,
                    UserQuestion.operation_id,
                ).where(UserQuestion.org_id == self.org_id, UserQuestion.id == question_id)
            )
        ).one_or_none()
        if identity is None:
            raise InteractionError("interaction_not_found")
        turn_id, execution_id, operation_id = identity
        turn = await self._turn(session, turn_id)
        await self._execution(session, turn_id, execution_id)
        operation = await self._operation(session, turn_id, execution_id, operation_id)
        question = await session.scalar(
            select(UserQuestion)
            .where(
                UserQuestion.org_id == self.org_id,
                UserQuestion.turn_id == turn_id,
                UserQuestion.operation_id == operation_id,
                UserQuestion.id == question_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if question is None:
            raise InteractionError("interaction_not_found")
        return turn, operation, question

    @staticmethod
    async def _now(session: AsyncSession) -> datetime:
        # PostgreSQL now() is transaction-start time, potentially BEFORE a long lock wait.
        return cast(datetime, (await session.execute(select(func.clock_timestamp()))).scalar_one())

    @staticmethod
    def _active(turn: InteractionTurn) -> None:
        if turn.status != "active":
            raise InteractionError(
                "user_input_cancelled" if turn.status == "cancelled" else "interaction_conflict"
            )

    @staticmethod
    def _notify(session: AsyncSession, question: UserQuestion, *, wake: bool) -> None:
        for kind in ("question_status", "question_available") if wake else ("question_status",):
            session.add(
                InteractionOutbox(
                    org_id=question.org_id,
                    turn_id=question.turn_id,
                    execution_id=question.execution_id,
                    question_id=question.id,
                    question_version=question.version,
                    event_kind=kind,
                )
            )

    async def reserve_operation(
        self,
        session: AsyncSession,
        execution_id: UUID,
        invocation_id: UUID,
        kind: str,
        arguments_hash: str,
        *,
        allow_new: bool = True,
    ) -> ToolOperation:
        if kind not in {
            "ask_user",
            "web_search",
            "web_read",
            "image_generate",
            "image_edit",
            "file_attach",
        } or not re.fullmatch(
            r"[0-9a-f]{64}", arguments_hash
        ):
            raise InteractionError("invalid_operation")
        turn_id = await session.scalar(
            select(InteractionExecution.turn_id).where(
                InteractionExecution.org_id == self.org_id,
                InteractionExecution.id == execution_id,
            )
        )
        if turn_id is None:
            raise InteractionError("interaction_not_found")
        turn = await self._turn(session, turn_id)
        await self._execution(session, turn_id, execution_id)
        existing = await session.scalar(
            select(ToolOperation)
            .where(
                ToolOperation.org_id == self.org_id,
                ToolOperation.execution_id == execution_id,
                ToolOperation.invocation_id == invocation_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing is not None:
            if existing.kind != kind or existing.arguments_hash != arguments_hash:
                raise InteractionError("interaction_conflict")
            return existing
        if not allow_new:
            raise InteractionError("user_input_disabled")
        self._active(turn)
        if kind == "file_attach":
            # Counted from the turn's operations under the turn lock: no turn
            # column, which the archive format would have to carry.
            attached = await session.scalar(
                select(func.count())
                .select_from(ToolOperation)
                .where(
                    ToolOperation.org_id == self.org_id,
                    ToolOperation.turn_id == turn_id,
                    ToolOperation.kind == "file_attach",
                )
            )
            if (attached or 0) >= MAX_FILES_PER_TURN:
                raise InteractionError("tool_limit")
            operation = ToolOperation(
                org_id=self.org_id,
                turn_id=turn_id,
                execution_id=execution_id,
                invocation_id=invocation_id,
                kind=kind,
                arguments_hash=arguments_hash,
            )
            session.add(operation)
            await session.flush()
            return operation
        limits = TurnLimits.model_validate(turn.limits)
        budget = {
            "ask_user": ("question_count", limits.question_total),
            "web_search": ("search_count", limits.web_search_total),
            "web_read": ("read_count", limits.web_read_total),
            # Generation and editing share one per-turn image budget.
            "image_generate": ("image_count", limits.image_total),
            "image_edit": ("image_count", limits.image_total),
        }
        counter, maximum = budget[kind]
        if getattr(turn, counter) >= maximum:
            raise InteractionError("user_input_limit" if kind == "ask_user" else "tool_limit")
        setattr(turn, counter, getattr(turn, counter) + 1)
        operation = ToolOperation(
            org_id=self.org_id,
            turn_id=turn_id,
            execution_id=execution_id,
            invocation_id=invocation_id,
            kind=kind,
            arguments_hash=arguments_hash,
        )
        session.add(operation)
        await session.flush()
        return operation

    async def create_question(
        self,
        session: AsyncSession,
        operation_id: UUID,
        request: QuestionRequest,
    ) -> UserQuestion:
        # Revalidate even a caller-mutated Pydantic instance before allocating a question.
        request = QuestionRequest.model_validate(request.model_dump())
        identity = (
            await session.execute(
                select(ToolOperation.turn_id, ToolOperation.execution_id).where(
                    ToolOperation.org_id == self.org_id,
                    ToolOperation.id == operation_id,
                )
            )
        ).one_or_none()
        if identity is None:
            raise InteractionError("interaction_not_found")
        turn_id, execution_id = identity
        turn = await self._turn(session, turn_id)
        await self._execution(session, turn_id, execution_id)
        operation = await self._operation(session, turn_id, execution_id, operation_id)
        if operation.kind != "ask_user" or operation.arguments_hash != question_arguments_hash(
            request
        ):
            raise InteractionError("interaction_conflict")
        existing = await session.scalar(
            select(UserQuestion)
            .where(
                UserQuestion.org_id == self.org_id,
                UserQuestion.operation_id == operation_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing is not None:
            return existing
        self._active(turn)
        if operation.status != "reserved":
            raise InteractionError("interaction_conflict")
        pending = (
            await session.scalars(
                select(UserQuestion.execution_id).where(
                    UserQuestion.org_id == self.org_id,
                    UserQuestion.turn_id == turn_id,
                    UserQuestion.status == "pending",
                )
            )
        ).all()
        limits = TurnLimits.model_validate(turn.limits)
        if execution_id in pending or len(pending) >= limits.question_open:
            raise InteractionError("user_input_limit")
        now = await self._now(session)
        question = UserQuestion(
            org_id=self.org_id,
            turn_id=turn_id,
            execution_id=execution_id,
            operation_id=operation_id,
            text=request.text,
            options=[option.model_dump() for option in request.options],
            created_at=now,
            expires_at=now + timedelta(seconds=limits.question_timeout_seconds),
        )
        session.add(question)
        operation.status = "running"
        await session.flush()
        self._notify(session, question, wake=False)
        await session.flush()
        return question

    async def answer_question(
        self,
        session: AsyncSession,
        question_id: UUID,
        actor_id: UUID,
        key: UUID,
        answer: AnswerRequest,
    ) -> UserQuestion:
        answer = AnswerRequest.model_validate(answer.model_dump())
        turn, operation, question = await self._question_context(session, question_id)
        if turn.owner_id != actor_id:
            raise InteractionError("interaction_not_found")
        payload = answer.model_dump(by_alias=True, exclude_none=True)
        if question.status == "answered":
            if question.answer_key == key and question.answer == payload:
                return question
            raise InteractionError("interaction_conflict")
        self._active(turn)
        now = await self._now(session)
        if question.status == "cancelled":
            raise InteractionError("user_input_cancelled")
        if not answer_allowed(question.status, turn.status == "active", now, question.expires_at):
            raise InteractionError("user_input_timeout")
        if answer.option_id is not None and answer.option_id not in {
            option["id"] for option in question.options
        }:
            raise InteractionError("invalid_answer")
        question.status = "answered"
        question.answer = payload
        question.answer_key = key
        question.answered_at = now
        question.answered_by = actor_id
        question.version += 1
        operation.status = "completed"
        operation.result = {"answer": payload}
        self._notify(session, question, wake=True)
        await session.flush()
        return question

    async def expire_question(self, session: AsyncSession, question_id: UUID) -> UserQuestion:
        turn, operation, question = await self._question_context(session, question_id)
        if question.status != "pending":
            return question
        now = await self._now(session)
        if turn.status == "active" and now < question.expires_at:
            return question
        question.status = "expired" if turn.status == "active" else "cancelled"
        question.version += 1
        operation.status = "failed" if question.status == "expired" else "cancelled"
        operation.error_code = (
            "user_input_timeout" if question.status == "expired" else "user_input_cancelled"
        )
        self._notify(session, question, wake=turn.status == "active")
        await session.flush()
        return question

    async def consume_question(self, session: AsyncSession, question_id: UUID) -> UserQuestion:
        turn, _, question = await self._question_context(session, question_id)
        self._active(turn)
        if question.status == "cancelled":
            raise InteractionError("user_input_cancelled")
        if question.status == "expired":
            raise InteractionError("user_input_timeout")
        if question.status != "answered" or question.answer is None:
            raise InteractionError("interaction_conflict")
        if question.consumed_at is None:
            # Wall time may move backwards between answer and consume. Keep
            # causal ordering under the same turn/row locks without changing
            # the answer deadline or weakening ck_user_question_consumed.
            if question.answered_at is None:
                raise InteractionError("interaction_conflict")
            question.consumed_at = max(await self._now(session), question.answered_at)
            await session.flush()
        return question

    async def cancel_turn(self, session: AsyncSession, turn_id: UUID) -> None:
        await self.finish_turn(session, turn_id, "cancelled")

    async def finish_turn(self, session: AsyncSession, turn_id: UUID, terminal_status: str) -> None:
        if terminal_status not in {"completed", "failed", "cancelled"}:
            raise InteractionError("interaction_conflict")
        turn = await self._turn(session, turn_id)
        if turn.status == terminal_status:
            return
        if turn.status != "active":
            raise InteractionError("interaction_conflict")
        now = await self._now(session)
        turn.status = terminal_status
        turn.version += 1
        turn.terminal_at = now
        # The turn lock serializes all writers; deterministic child ordering also avoids
        # lock inversion with future maintenance paths following this same contract.
        operations = (
            await session.scalars(
                select(ToolOperation)
                .where(
                    ToolOperation.org_id == self.org_id,
                    ToolOperation.turn_id == turn_id,
                    ToolOperation.status.in_(("reserved", "running")),
                )
                .order_by(ToolOperation.execution_id, ToolOperation.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        if terminal_status == "completed" and operations:
            raise InteractionError("interaction_conflict")
        for operation in operations:
            operation.status = "cancelled"
            operation.error_code = "user_input_cancelled"
        completed_web_operations = (
            await session.scalars(
                select(ToolOperation)
                .where(
                    ToolOperation.org_id == self.org_id,
                    ToolOperation.turn_id == turn_id,
                    ToolOperation.kind.in_(("web_search", "web_read")),
                    ToolOperation.status == "completed",
                    ToolOperation.result.is_not(None),
                    ToolOperation.result_expires_at.is_(None),
                )
                .order_by(ToolOperation.execution_id, ToolOperation.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        for operation in completed_web_operations:
            operation.result_expires_at = now + timedelta(hours=24)
        questions = (
            await session.scalars(
                select(UserQuestion)
                .where(
                    UserQuestion.org_id == self.org_id,
                    UserQuestion.turn_id == turn_id,
                    UserQuestion.status == "pending",
                )
                .order_by(UserQuestion.execution_id, UserQuestion.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        if terminal_status == "completed" and questions:
            raise InteractionError("interaction_conflict")
        for question in questions:
            question.status = "cancelled"
            question.version += 1
            self._notify(session, question, wake=False)
        await session.flush()
