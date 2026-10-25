"""Reviewed alternate unique keys; UUID-based constraints are checked separately.

Predicates mirror the two partial unique indexes. SQL's operator-only probe owns
cross-tenant visibility and typed equality, including ordinary NULL-distinct keys.
"""

from typing import Any
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.services.archive.schemas import canonical

UNIQUE_KEYS: dict[str, list[tuple[str, ...]]] = {
    "knowledge_bases": [("org_id", "name"), ("owning_conversation_id",)],
    "skills": [("owning_conversation_id",)],
    "file_metadata": [("storage_key",)],
    "workflow_runs": [("temporal_workflow_id",), ("conversation_id", "turn_id")],
    "workflow_node_runs": [("workflow_run_id", "phase_key")],
    "interaction_turns": [("org_id", "conversation_id")],
    "interaction_executions": [("org_id", "turn_id", "workflow_id", "run_id")],
    "tool_operations": [("org_id", "execution_id", "invocation_id")],
    "user_questions": [("org_id", "operation_id"), ("org_id", "execution_id")],
    "interaction_outbox": [("org_id", "question_id", "question_version", "event_kind")],
    "web_sources": [("org_id", "operation_id", "position")],
    "conversation_images": [("storage_key",), ("org_id", "operation_id")],
}


async def unique_collision(session: AsyncSession, table: str, row: dict[str, Any]) -> bool:
    if table not in UNIQUE_KEYS:
        return False
    columns = {column for key in UNIQUE_KEYS[table] for column in key}
    if table in ("interaction_turns", "user_questions"):
        columns.add("status")
    values = {
        name: str(row[name])
        if row[name] is not None and not isinstance(row[name], (str, int))
        else row[name]
        for name in columns
    }
    return bool(
        await session.scalar(
            text("SELECT platform_archive_unique_collision(:t,:id,CAST(:v AS jsonb))"),
            {"t": table, "id": row["id"], "v": canonical(values)},
        )
    )
