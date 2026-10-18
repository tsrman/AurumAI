import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.conversation import Conversation
from src.models.message import Message
from src.models.organization import Organization
from src.models.user import User
from src.repositories.message_repository import message_repository


async def _conversation(db: AsyncSession, org: Organization, user: User) -> Conversation:
    conv = Conversation(
        org_id=org.id,
        user_id=user.id,
        title="Ordering",
        model="gpt-4o-mini",
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return conv


def _message(
    conv: Conversation,
    org: Organization,
    turn_id: uuid.UUID,
    role: str,
    content: str,
    created_at: datetime,
) -> Message:
    return Message(
        conversation_id=conv.id,
        org_id=org.id,
        turn_id=turn_id,
        role=role,
        content=content,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_list_by_conversation_keeps_the_question_above_its_own_answer(
    db: AsyncSession, test_org: Organization, test_user: User
) -> None:
    """Regression test: the chat history rendered an assistant reply above
    the user message it was answering.

    created_at is `now()` = transaction_timestamp(), so the user row (written
    by /v1/chat/stream) and the assistant row (written by the runner's
    POST /conversations/{id}/messages) carry the start times of two unrelated
    transactions. Twice in a live database that came out inverted by ~100-175ms
    and the UI, which renders the API's order verbatim
    (business-ui/src/hooks/useChat.ts), showed the answer first.

    The timestamps below are the inverted ones on purpose — the ordering must
    come from the turn structure, not from comparing the two clocks.
    """
    conv = await _conversation(db, test_org, test_user)
    turn_id = uuid.uuid4()
    base = datetime.now(timezone.utc)

    db.add(
        _message(conv, test_org, turn_id, "assistant", "answer", base)
    )
    db.add(
        _message(
            conv, test_org, turn_id, "user", "question", base + timedelta(milliseconds=175)
        )
    )
    await db.commit()

    messages = await message_repository.list_by_conversation(db, str(conv.id))

    assert [(m.role, m.content) for m in messages] == [
        ("user", "question"),
        ("assistant", "answer"),
    ]


@pytest.mark.asyncio
async def test_list_by_conversation_keeps_turns_in_chronological_order(
    db: AsyncSession, test_org: Organization, test_user: User
) -> None:
    """Fixing the within-turn order must not reshuffle the turns themselves:
    a turn is placed by its earliest row, so an inverted pair still sits where
    its question was asked."""
    conv = await _conversation(db, test_org, test_user)
    first_turn, second_turn = uuid.uuid4(), uuid.uuid4()
    base = datetime.now(timezone.utc)

    db.add(_message(conv, test_org, first_turn, "user", "q1", base))
    db.add(_message(conv, test_org, first_turn, "assistant", "a1", base + timedelta(seconds=3)))
    # Second turn, inverted the way production produced it.
    db.add(
        _message(
            conv, test_org, second_turn, "assistant", "a2", base + timedelta(seconds=10)
        )
    )
    db.add(
        _message(
            conv,
            test_org,
            second_turn,
            "user",
            "q2",
            base + timedelta(seconds=10, milliseconds=175),
        )
    )
    await db.commit()

    messages = await message_repository.list_by_conversation(db, str(conv.id))

    assert [m.content for m in messages] == ["q1", "a1", "q2", "a2"]


@pytest.mark.asyncio
async def test_list_by_conversation_orders_rows_without_a_turn_id_by_created_at(
    db: AsyncSession, test_org: Organization, test_user: User
) -> None:
    """turn_id is nullable and older rows predate it. Those must not be
    collapsed into one shared group by the PARTITION BY (SQL groups all NULLs
    together) — the coalesce to the row's own id is what prevents that, and
    they keep their plain created_at order."""
    conv = await _conversation(db, test_org, test_user)
    base = datetime.now(timezone.utc)

    for offset, (role, content) in enumerate(
        [("user", "old-q"), ("assistant", "old-a"), ("user", "old-q2")]
    ):
        db.add(
            Message(
                conversation_id=conv.id,
                org_id=test_org.id,
                turn_id=None,
                role=role,
                content=content,
                created_at=base + timedelta(seconds=offset),
            )
        )
    await db.commit()

    messages = await message_repository.list_by_conversation(db, str(conv.id))

    assert [m.content for m in messages] == ["old-q", "old-a", "old-q2"]


@pytest.mark.asyncio
async def test_upsert_merges_extra_instead_of_replacing_the_evaluator_score(
    db: AsyncSession, test_org: Organization, test_user: User
) -> None:
    """A retried persist must not drop keys another path wrote into `extra`.

    `extra` is a plain JSON column, so `on_conflict_do_update` can only write
    it wholesale. The evaluator's faithfulness score lands in that same dict
    via update_score_by_turn, so replacing it on a retried
    POST /conversations/{id}/messages would silently erase the score.
    """
    conv = await _conversation(db, test_org, test_user)
    turn_id = uuid.uuid4()

    await message_repository.upsert_assistant_by_turn(
        db,
        conversation_id=str(conv.id),
        turn_id=turn_id,
        role="assistant",
        content="answer",
        org_id=str(test_org.id),
    )
    await db.commit()

    scored = await message_repository.update_score_by_turn(
        db,
        conversation_id=str(conv.id),
        turn_id=str(turn_id),
        role="assistant",
        score=0.75,
        reason="grounded",
    )
    assert scored is not None

    merged = await message_repository.upsert_assistant_by_turn(
        db,
        conversation_id=str(conv.id),
        turn_id=turn_id,
        role="assistant",
        content="answer",
        org_id=str(test_org.id),
        extra={"web_source_ids": ["s1"], "unsupported_web_source_ids": []},
    )
    await db.commit()

    assert merged.extra == {
        "faithfulness_score": 0.75,
        "faithfulness_reason": "grounded",
        "web_source_ids": ["s1"],
        "unsupported_web_source_ids": [],
    }
