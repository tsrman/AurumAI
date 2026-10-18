from typing import Any, List, Optional
from uuid import UUID, uuid4

from sqlalchemy import case, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.models.message import Message
from src.models.data_identity import ConversationIdentity, MessageIdentity
from src.repositories.base import BaseRepository


class MessageRepository(BaseRepository[Message]):
    def __init__(self) -> None:
        super().__init__(Message)

    async def get(self, session: AsyncSession, obj_id: str) -> Optional[Message]:
        try:
            key = UUID(obj_id)
        except (ValueError, TypeError, AttributeError):
            return None
        timestamp = await session.scalar(select(MessageIdentity.created_at).where(MessageIdentity.id == key))
        if timestamp is None:
            return None
        return await session.get(Message, {"id": key, "created_at": timestamp})

    async def list_by_conversation(
        self,
        session: AsyncSession,
        conversation_id: str,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> List[Message]:
        # Ordering by created_at alone put an assistant reply *above* the
        # question it answered (2 turns out of 39 in a live database, off by
        # 108ms and 175ms). created_at is `func.now()` — Postgres'
        # transaction_timestamp(), the moment the writing transaction
        # *began*, not the moment the row was inserted — and the two halves
        # of one turn are written by different services in different
        # transactions: the user row by the browser's POST /v1/chat/stream,
        # the assistant row by the runner's POST
        # /conversations/{id}/messages. Two independent transaction start
        # times are not a reliable event order at sub-second distances, so a
        # turn could come back inverted.
        #
        # Hence the order is derived from the turn, not from a wall clock:
        # turns by when their earliest row was written, and within a turn the
        # roles in their fixed conversational order. turn_id is unique per
        # turn (uq_messages_conv_turn_role), so grouping on it cannot merge
        # two turns; a row written without one falls back to its own id,
        # which leaves it in a group of one, ordered by created_at as before.
        turn_key = func.coalesce(Message.turn_id, Message.id)
        turn_started_at = func.min(Message.created_at).over(partition_by=turn_key)
        role_order = case(
            (Message.role == "system", 0),
            (Message.role == "user", 1),
            (Message.role == "assistant", 2),
            else_=3,
        )
        # Compute turn order over the whole conversation before applying the
        # cursor. Filtering first would change window minima at page boundaries.
        ordered = (
            select(
                Message.id,
                Message.created_at,
                turn_started_at.label("turn_started_at"),
                turn_key.label("turn_key"),
                role_order.label("role_order"),
            )
            .where(Message.conversation_id == conversation_id)
            .cte("ordered_messages")
        )
        ordering = (
            ordered.c.turn_started_at,
            ordered.c.turn_key,
            ordered.c.role_order,
            ordered.c.created_at,
            ordered.c.id,
        )
        stmt = select(Message).join(ordered, Message.id == ordered.c.id).order_by(*ordering)
        if cursor is not None:
            # Resolve the anchor only inside this conversation. A missing/deleted
            # or foreign message must not silently restart pagination.
            anchor = (await session.execute(
                select(*ordering).where(ordered.c.id == UUID(cursor))
            )).one_or_none()
            if anchor is None:
                raise ValueError("Invalid message history cursor")
            stmt = stmt.where(tuple_(*ordering) > tuple_(*anchor))
        stmt = stmt.limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def update_content(
        self,
        session: AsyncSession,
        message_id: str,
        content: str,
    ) -> Message:
        message = await self.get(session, message_id)
        if message is None:
            raise ValueError(f"Message {message_id} not found")
        from src.services.conversation_lifecycle import lock_active_conversation
        await lock_active_conversation(session, message.org_id, message.conversation_id)
        message.content = content
        await session.commit()
        await session.refresh(message)
        return message

    async def upsert_assistant_by_turn(
        self,
        session: AsyncSession,
        conversation_id: str,
        turn_id: UUID | str | None,
        role: str,
        content: str,
        org_id: str | None = None,
        model: str | None = None,
        tool_calls: list[Any] | dict[str, Any] | None = None,
        tool_results: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Message:
        # Lock the live payload before the registry, matching parent DELETE's
        # row-lock order. The registry alone is also present after physical deletion.
        owner_org = org_id
        if owner_org is None:
            owner_org_value = await session.scalar(select(ConversationIdentity.org_id).where(
                ConversationIdentity.id == UUID(conversation_id),
            ))
            if owner_org_value is None:
                raise ValueError("Conversation identity unavailable")
            owner_org = str(owner_org_value)
        from src.services.conversation_lifecycle import lock_active_conversation
        owner = await lock_active_conversation(session, UUID(owner_org), UUID(conversation_id))
        reservation = insert(MessageIdentity).values(
            id=uuid4(), org_id=owner.org_id, conversation_id=owner.id,
            turn_id=turn_id, role=role,
        ).on_conflict_do_nothing(index_elements=["conversation_id", "turn_id", "role"]).returning(
            MessageIdentity.id, MessageIdentity.created_at,
        )
        identity = (await session.execute(reservation)).one_or_none()
        if identity is None:
            identity = (await session.execute(select(MessageIdentity.id, MessageIdentity.created_at).where(
                MessageIdentity.conversation_id == owner.id, MessageIdentity.turn_id == turn_id,
                MessageIdentity.role == role, MessageIdentity.lifecycle == "active",
            ))).one()
        update_values: dict[str, Any] = {
            "content": content,
            "model": model,
            "tool_calls": tool_calls or [],
            "tool_results": tool_results or {},
        }
        stmt = (
            insert(Message)
            .values(
                id=identity.id,
                created_at=identity.created_at,
                conversation_id=conversation_id,
                org_id=owner.org_id,
                turn_id=turn_id,
                role=role,
                content=content,
                model=model,
                tool_calls=tool_calls or [],
                tool_results=tool_results or {},
                extra=extra or {},
            )
            .on_conflict_do_update(
                index_elements=["id", "created_at"],
                set_=update_values,
            )
            .returning(Message)
            .execution_options(populate_existing=True)
        )
        result = await session.execute(stmt)
        message = result.scalar_one()
        if extra:
            # `extra` is a plain JSON column, so there is no server-side merge
            # operator to put in `set_`. Other paths write their own keys into
            # the same dict (update_score_by_turn's faithfulness score), so a
            # retried persist must merge rather than replace, or it silently
            # drops whatever landed between the two calls.
            merged = {**(message.extra or {}), **extra}
            if merged != message.extra:
                message.extra = merged
                await session.flush()
        return message

    async def update_score_by_turn(
        self,
        session: AsyncSession,
        conversation_id: str,
        turn_id: UUID | str,
        role: str,
        score: float | None,
        reason: str | None,
    ) -> Optional[Message]:
        owner_org = await session.scalar(select(ConversationIdentity.org_id).where(
            ConversationIdentity.id == UUID(conversation_id),
        ))
        if owner_org is None:
            return None
        from src.services.conversation_lifecycle import lock_active_conversation
        await lock_active_conversation(session, owner_org, UUID(conversation_id))
        stmt = select(Message).where(
            Message.conversation_id == conversation_id,
            Message.turn_id == turn_id,
            Message.role == role,
        )
        result = await session.execute(stmt)
        message = result.scalar_one_or_none()
        if message is None:
            return None
        extra = dict(message.extra or {})
        extra["faithfulness_score"] = score
        extra["faithfulness_reason"] = reason
        message.extra = extra
        await session.commit()
        await session.refresh(message)
        return message


message_repository = MessageRepository()
