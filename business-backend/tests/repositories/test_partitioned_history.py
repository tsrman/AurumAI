"""Public UUID APIs and turn ordering are stable across physical months."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from src.models.conversation import Conversation
from src.models.message import Message
from src.repositories.conversation_repository import conversation_repository
from src.repositories.message_repository import message_repository


@pytest.mark.asyncio
async def test_registry_retry_updates_original_month_and_merges_feedback(db, test_org, test_user):
    conv = Conversation(org_id=test_org.id, user_id=test_user.id, model="test")
    db.add(conv)
    await db.flush()
    turn = uuid.uuid4()
    original = Message(
        org_id=test_org.id,
        conversation_id=conv.id,
        turn_id=turn,
        role="assistant",
        content="first",
        feedback="like",
        extra={"faithfulness_score": 0.75},
        created_at=datetime(2024, 1, 31, 23, 59, tzinfo=timezone.utc),
    )
    db.add(original)
    await db.flush()
    retried = await message_repository.upsert_assistant_by_turn(
        db, str(conv.id), turn, "assistant", "second", org_id=str(test_org.id), extra={"new": True}
    )
    assert retried.id == original.id
    assert retried.created_at == original.created_at
    assert retried.content == "second"
    assert retried.feedback == "like"
    assert retried.extra == {"faithfulness_score": 0.75, "new": True}
    resolved = await message_repository.get(db, str(original.id))
    assert resolved is not None and resolved.content == "second"
    assert await conversation_repository.get(db, str(conv.id)) is conv
    assert (
        await db.execute(text("SELECT count(*) FROM messages WHERE turn_id=:turn"), {"turn": turn})
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_52_message_history_paginates_across_months(db, test_org, test_user):
    # A conversation begins before its first message.
    conv = Conversation(
        org_id=test_org.id,
        user_id=test_user.id,
        model="test",
        created_at=datetime(2024, 1, 30, tzinfo=timezone.utc),
    )
    db.add(conv)
    await db.flush()
    expected = []
    start = datetime(2024, 1, 31, 12, tzinfo=timezone.utc)
    for i in range(26):
        turn = uuid.uuid4()
        for role in ("user", "assistant"):
            msg = Message(
                org_id=test_org.id,
                conversation_id=conv.id,
                turn_id=turn,
                role=role,
                content=f"{i}:{role}",
                created_at=start + timedelta(hours=i, seconds=1 if role == "user" else 0),
            )
            db.add(msg)
            await db.flush()
            expected.append(msg.id)
    actual = []
    cursor = None
    while True:
        page = await message_repository.list_by_conversation(
            db, str(conv.id), limit=7, cursor=cursor
        )
        if not page:
            break
        actual.extend(row.id for row in page)
        cursor = str(page[-1].id)
    assert actual == expected


@pytest.mark.asyncio
async def test_nullable_turn_preserves_distinct_messages(db, test_org, test_user):
    conv = Conversation(org_id=test_org.id, user_id=test_user.id, model="test")
    db.add(conv)
    await db.flush()
    first = await message_repository.upsert_assistant_by_turn(
        db, str(conv.id), None, "assistant", "one", org_id=str(test_org.id)
    )
    second = await message_repository.upsert_assistant_by_turn(
        db, str(conv.id), None, "assistant", "two", org_id=str(test_org.id)
    )
    assert first.id != second.id


def _record_statements() -> tuple[list[str], object]:
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        seen.append(" ".join(statement.split()))

    event.listen(Engine, "before_cursor_execute", record)
    return seen, lambda: event.remove(Engine, "before_cursor_execute", record)  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_history_reads_only_the_months_since_the_conversation_began(db, test_org, test_user):
    """`messages` is partitioned by month and a conversation's messages carry no partition
    hint, so without a lower bound every read probes every partition ever created (planning
    alone grows with their number: 8 ms at 46 partitions, 0.2 ms with the bound)."""
    conv = Conversation(
        org_id=test_org.id,
        user_id=test_user.id,
        model="test",
        created_at=datetime(2024, 1, 30, tzinfo=timezone.utc),
    )
    db.add(conv)
    await db.flush()
    db.add(
        Message(
            org_id=test_org.id,
            conversation_id=conv.id,
            turn_id=uuid.uuid4(),
            role="user",
            content="hi",
            created_at=datetime(2024, 2, 1, tzinfo=timezone.utc),
        )
    )
    await db.flush()

    seen, stop = _record_statements()
    try:
        page = await message_repository.list_by_conversation(db, str(conv.id), limit=10)
    finally:
        stop()  # type: ignore[operator]

    assert [m.content for m in page] == ["hi"]
    history = [s for s in seen if "FROM messages" in s]
    assert history, seen
    for sql in history:
        assert "messages.created_at >=" in sql, sql
    # The row is fetched by its whole key, so only its own partition is read.
    assert any("messages.created_at = ordered_messages.created_at" in s for s in history), history
