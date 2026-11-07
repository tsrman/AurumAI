"""The sidebar's conversation list must be answered from an index.

The list is one user's conversations, newest activity first. Without an index
that starts with ``(org_id, user_id)`` it reads every partition of the table.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

INDEX = "ix_conversations_user_activity"


@pytest.mark.asyncio
async def test_the_user_activity_index_exists_on_the_partitioned_table(db: AsyncSession) -> None:
    definition = await db.scalar(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() AND tablename = 'conversations' AND indexname = :n"
        ),
        {"n": INDEX},
    )

    assert definition is not None
    assert "org_id, user_id" in definition and "COALESCE(last_message_at, created_at)" in definition


@pytest.mark.asyncio
async def test_the_conversation_list_query_uses_it(db: AsyncSession) -> None:
    # Make the planner avoid a scan or a sort whenever the schema offers a way not to
    # need one, so the plan shows what the indexes can do, not what the stats prefer.
    await db.execute(text("SET LOCAL enable_seqscan = off"))
    await db.execute(text("SET LOCAL enable_sort = off"))
    plan = await db.scalars(
        text(
            "EXPLAIN SELECT c.id FROM conversations c "
            "WHERE c.org_id = :org AND c.user_id = :user AND c.status != 'deleted' "
            "ORDER BY coalesce(c.last_message_at, c.created_at) DESC, c.id DESC LIMIT 21"
        ),
        {"org": uuid.uuid4(), "user": uuid.uuid4()},
    )
    text_plan = "\n".join(plan.all())

    # Child indexes are named after their columns, not after the parent index.
    # Rows must come out of the index already ordered: no Sort node, no table scan.
    assert "Index Scan" in text_plan and "Seq Scan" not in text_plan
    assert "Sort" not in text_plan.replace("Sort Key", "")
