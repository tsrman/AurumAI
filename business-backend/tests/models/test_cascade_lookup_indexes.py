"""Deleting a conversation looks up what it owns in every child table.

Each child table needs an index that starts with the foreign key's columns, or the
delete scans the whole table once per child table.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# (table, column holding the conversation id)
CHILDREN = [
    ("interaction_turns", "conversation_id"),
    ("conversation_executions", "conversation_id"),
    ("hermes_sessions", "conversation_id"),
    ("archive_jobs", "conversation_id"),
    ("file_metadata", "owning_conversation_id"),
]

# Single-column org_id indexes that the migration drops because another index on the table
# already starts with org_id.
DROPPED = [
    ("interaction_turns", "ix_interaction_turns_org_id"),
    ("conversation_executions", "ix_conversation_executions_org_id"),
    ("hermes_sessions", "ix_hermes_sessions_org_id"),
    ("tool_operations", "ix_tool_operations_org_id"),
    ("conversations", "ix_conversations_org_id_copy"),
    ("audit_logs", "ix_audit_logs_org_id_copy"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("table", "column"), CHILDREN)
async def test_the_conversation_lookup_uses_an_index(
    db: AsyncSession, table: str, column: str
) -> None:
    await db.execute(text("SET LOCAL enable_seqscan = off"))
    plan = await db.scalars(
        text(f"EXPLAIN SELECT 1 FROM {table} WHERE org_id = :org AND {column} = :conv"),
        {"org": uuid.uuid4(), "conv": uuid.uuid4()},
    )
    lines = "\n".join(plan.all())

    assert "Seq Scan" not in lines, lines
    # The conversation column must be part of the index condition, not a filter applied
    # after reading every row of the organization.
    conditions = [ln for ln in lines.splitlines() if "Index Cond" in ln]
    assert conditions and any(column in ln for ln in conditions), lines


@pytest.mark.asyncio
@pytest.mark.parametrize(("table", "index"), DROPPED)
async def test_a_dropped_org_id_index_is_covered_by_another_that_starts_with_org_id(
    db: AsyncSession, table: str, index: str
) -> None:
    exists = await db.scalar(
        text("SELECT count(*) FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :i"),
        {"i": index},
    )
    assert exists == 0

    covering = await db.scalar(
        text(
            """
            SELECT count(*) FROM pg_index i
            JOIN pg_class t ON t.oid = i.indrelid
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = i.indkey[0]
            WHERE t.relname = :t AND a.attname = 'org_id'
              AND i.indpred IS NULL AND i.indnatts > 1 AND i.indisvalid
            """
        ),
        {"t": table},
    )
    assert covering and covering >= 1, f"{table} would be left with no index starting with org_id"
