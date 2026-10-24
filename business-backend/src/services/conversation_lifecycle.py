"""Payload-first lifecycle locking and durable external-operation fencing.

All helpers leave the transaction to their caller. The external operation intent
MUST be committed before I/O; errors and lease expiry never settle an intent.
"""

from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import exists, select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.conversation_image import ConversationImage
from src.models.archive_control import ConversationExecution, ConversationOperation
from src.models.conversation import Conversation
from src.models.data_identity import ConversationIdentity
from src.models.interaction import InteractionOutbox, InteractionTurn, ToolOperation, UserQuestion
from src.models.message import Message
from src.models.llm_usage import LLMUsageEvent
from src.models.workflow_run import WorkflowRun

OperationKind = Literal[
    "workflow_signal",
    "workflow_start",
    "document_upload",
    "document_create",
    "document_delete",
    "runner_start",
]


class ConversationFrozen(HTTPException):
    def __init__(self) -> None:
        super().__init__(409, "Conversation frozen")


class ArchiveBlocked(Exception):
    """Safe bounded reason code; never includes external payloads."""


class AttachmentFence(Protocol):
    """Task2 must establish its own durable external receipt before settling."""

    async def settle(self, session: AsyncSession, operation: ConversationOperation) -> None: ...


async def lock_conversation_identity(
    session: AsyncSession, org_id: UUID, conversation_id: UUID
) -> tuple[ConversationIdentity, Conversation | None]:
    at = await session.scalar(
        select(ConversationIdentity.created_at).where(
            ConversationIdentity.id == conversation_id, ConversationIdentity.org_id == org_id
        )
    )
    if at is None:
        raise HTTPException(404, "Conversation not found")
    payload = await session.scalar(
        select(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.org_id == org_id,
            Conversation.created_at == at,
        )
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    identity = await session.scalar(
        select(ConversationIdentity)
        .where(ConversationIdentity.id == conversation_id, ConversationIdentity.org_id == org_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if identity is None:
        raise HTTPException(404, "Conversation not found")
    return identity, payload


async def lock_active_conversation(
    session: AsyncSession, org_id: UUID, conversation_id: UUID, *, generation: int | None = None
) -> ConversationIdentity:
    identity, payload = await lock_conversation_identity(session, org_id, conversation_id)
    if identity.lifecycle != "active" or (
        generation is not None and identity.generation != generation
    ):
        raise ConversationFrozen()
    if payload is None:
        raise HTTPException(404, "Conversation not found")
    return identity


async def begin_operation(
    session: AsyncSession,
    org_id: UUID,
    conversation_id: UUID,
    *,
    kind: OperationKind,
    operation_key: UUID,
    knowledge_base_id: UUID | None = None,
    resource_key: str | None = None,
) -> ConversationOperation:
    identity = await lock_active_conversation(session, org_id, conversation_id)
    operation = await session.scalar(
        select(ConversationOperation).where(
            ConversationOperation.org_id == org_id,
            ConversationOperation.conversation_id == conversation_id,
            ConversationOperation.generation == identity.generation,
            ConversationOperation.kind == kind,
            ConversationOperation.operation_key == operation_key,
        )
    )
    if operation is None:
        operation = ConversationOperation(
            org_id=org_id,
            conversation_id=conversation_id,
            generation=identity.generation,
            kind=kind,
            operation_key=operation_key,
            knowledge_base_id=knowledge_base_id,
            resource_key=resource_key,
        )
        session.add(operation)
        if kind in ("document_upload", "document_create", "document_delete"):
            # The parent lock above serializes activity with archive eligibility.
            # Only a new intent touches retention; a stable-key retry does not.
            payload = await session.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id, Conversation.org_id == org_id
                )
            )
            if payload is None:
                raise ConversationFrozen()
            payload.updated_at = datetime.now(timezone.utc)
        await session.flush()
    elif operation.knowledge_base_id != knowledge_base_id or operation.resource_key != resource_key:
        raise ConversationFrozen()
    return operation


async def settle_operation(
    session: AsyncSession,
    operation: ConversationOperation,
    *,
    workflow_id: str | None = None,
    run_id: UUID | None = None,
) -> None:
    await lock_active_conversation(
        session, operation.org_id, operation.conversation_id, generation=operation.generation
    )
    if operation.status == "settled":
        if (operation.workflow_id, operation.run_id) != (workflow_id, run_id):
            raise ConversationFrozen()
        return
    operation.status = "settled"
    operation.settled_at = datetime.now(timezone.utc)
    operation.workflow_id = workflow_id
    operation.run_id = run_id
    await session.flush()


async def eligibility_blocker(
    session: AsyncSession, identity: ConversationIdentity, payload: Conversation, cutoff: datetime
) -> str | None:
    if (
        max(
            value
            for value in (
                payload.created_at,
                payload.updated_at,
                payload.last_message_at,
                identity.restored_at,
            )
            if value is not None
        )
        >= cutoff
    ):
        return "recent_activity"
    owner = (
        InteractionTurn.org_id == identity.org_id,
        InteractionTurn.conversation_id == identity.id,
    )
    checks = [
        (
            "unsettled_billing_request",
            select(LLMUsageEvent.id).where(
                LLMUsageEvent.org_id == identity.org_id,
                LLMUsageEvent.conversation_id == identity.id,
                LLMUsageEvent.status != "completed",
            ),
        ),
        (
            "unsettled_operation",
            select(ConversationOperation.id).where(
                ConversationOperation.org_id == identity.org_id,
                ConversationOperation.conversation_id == identity.id,
                ConversationOperation.status != "settled",
            ),
        ),
        (
            "active_turn",
            select(InteractionTurn.id).where(*owner, InteractionTurn.status == "active"),
        ),
        (
            "active_workflow_run",
            select(WorkflowRun.id).where(
                WorkflowRun.org_id == identity.org_id,
                WorkflowRun.conversation_id == identity.id,
                WorkflowRun.status.notin_(["completed", "failed", "cancelled"]),
            ),
        ),
        (
            "pending_question",
            select(UserQuestion.id)
            .join(InteractionTurn, UserQuestion.turn_id == InteractionTurn.id)
            .where(*owner, UserQuestion.status == "pending"),
        ),
        (
            "unsettled_outbox",
            select(InteractionOutbox.id)
            .join(InteractionTurn, InteractionOutbox.turn_id == InteractionTurn.id)
            .where(
                *owner,
                InteractionOutbox.status == "pending",
                or_(
                    InteractionOutbox.event_kind != "question_status",
                    InteractionTurn.status == "active",
                ),
            ),
        ),
        (
            "unsettled_tool_operation",
            select(ToolOperation.id)
            .join(InteractionTurn, ToolOperation.turn_id == InteractionTurn.id)
            .where(*owner, ToolOperation.status.in_(["reserved", "running"])),
        ),
        (
            # The archive format does not carry image bytes yet; keeping such
            # conversations live avoids purging images that a restore could
            # not bring back.
            "conversation_images",
            select(ConversationImage.id).where(
                ConversationImage.org_id == identity.org_id,
                ConversationImage.conversation_id == identity.id,
            ),
        ),
        (
            "execution_not_closed",
            select(ConversationExecution.id).where(
                ConversationExecution.org_id == identity.org_id,
                ConversationExecution.conversation_id == identity.id,
                ConversationExecution.tree_complete.is_(False),
            ),
        ),
    ]
    for reason, query in checks:
        if await session.scalar(select(exists(query))):
            return reason
    history_present = bool(
        await session.scalar(
            select(exists(select(Message.id).where(Message.conversation_id == identity.id)))
        )
    ) or bool(await session.scalar(select(exists(select(InteractionTurn.id).where(*owner)))))
    if history_present and not identity.execution_provenance_complete:
        return "legacy_execution_provenance_missing"
    roots = await session.scalar(
        select(
            exists(
                select(ConversationExecution.id).where(
                    ConversationExecution.org_id == identity.org_id,
                    ConversationExecution.conversation_id == identity.id,
                    ConversationExecution.parent_run_id.is_(None),
                )
            )
        )
    )
    if not roots and history_present:
        return "legacy_execution_provenance_missing"
    return None


async def claim_conversation(
    session: AsyncSession,
    org_id: UUID,
    conversation_id: UUID,
    *,
    archive_id: UUID,
    lease_id: UUID,
    cutoff: datetime,
) -> ConversationIdentity:
    identity, payload = await lock_conversation_identity(session, org_id, conversation_id)
    if identity.lifecycle != "active" or payload is None:
        raise ArchiveBlocked("not_active")
    await prepare_conversation_snapshot(session, org_id, conversation_id)
    blocker = await eligibility_blocker(session, identity, payload, cutoff)
    if blocker:
        raise ArchiveBlocked(blocker)
    identity.lifecycle = "archiving"
    identity.generation += 1
    identity.archive_id = archive_id
    identity.lease_id = lease_id
    identity.lease_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    await session.flush()
    return identity


async def begin_attachment_operation(
    session: AsyncSession,
    org_id: UUID,
    knowledge_base_id: UUID,
    kind: OperationKind,
    operation_key: UUID,
    *,
    resource_key: str | None = None,
) -> ConversationOperation | None:
    from src.models.knowledge_base import KnowledgeBase

    owner = await session.scalar(
        select(KnowledgeBase.owning_conversation_id).where(
            KnowledgeBase.id == knowledge_base_id, KnowledgeBase.org_id == org_id
        )
    )
    if owner is None:
        return None
    operation = await begin_operation(
        session,
        org_id,
        owner,
        kind=kind,
        operation_key=operation_key,
        knowledge_base_id=knowledge_base_id,
        resource_key=resource_key,
    )
    await session.commit()
    return operation


async def prepare_conversation_snapshot(
    session: AsyncSession, org_id: UUID, conversation_id: UUID
) -> None:
    """Call before sealing. Due private fields are removed in the claim transaction.

    Transient web result bodies and diagnostics are excluded from archive schema
    regardless of expiry. The frozen SQL fence prevents background maintenance
    from subsequently changing a sealed snapshot.
    """
    from sqlalchemy import update
    from src.models.web_source import WebSource
    from src.models.workflow_run import WorkflowNodeRun

    await lock_active_conversation(session, org_id, conversation_id)
    now = datetime.now(timezone.utc)
    turns = select(InteractionTurn.id).where(
        InteractionTurn.org_id == org_id, InteractionTurn.conversation_id == conversation_id
    )
    expired = select(ToolOperation.id).where(
        ToolOperation.org_id == org_id,
        ToolOperation.turn_id.in_(turns),
        ToolOperation.kind.in_(["web_search", "web_read"]),
        ToolOperation.result_expires_at <= now,
    )
    await session.execute(
        update(WebSource)
        .where(WebSource.org_id == org_id, WebSource.operation_id.in_(expired))
        .values(content=None)
    )
    await session.execute(
        update(ToolOperation).where(ToolOperation.id.in_(expired)).values(result=None)
    )
    runs = select(WorkflowRun.id).where(
        WorkflowRun.org_id == org_id, WorkflowRun.conversation_id == conversation_id
    )
    await session.execute(
        update(WorkflowNodeRun)
        .where(
            WorkflowNodeRun.org_id == org_id,
            WorkflowNodeRun.workflow_run_id.in_(runs),
            WorkflowNodeRun.diagnostic_expires_at <= now,
        )
        .values(output=None, internal_error=None)
    )
    terminal_turns = turns.where(InteractionTurn.status != "active")
    await session.execute(
        update(InteractionOutbox)
        .where(
            InteractionOutbox.org_id == org_id,
            InteractionOutbox.turn_id.in_(terminal_turns),
            InteractionOutbox.event_kind == "question_status",
            InteractionOutbox.status == "pending",
        )
        .values(
            status="cancelled", error_code="terminal_turn_obsolete", lease_id=None, lease_until=None
        )
    )
    await session.flush()
