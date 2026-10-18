"""Versioned, bounded FK graph inventory. Ordinary definitions and grants stay live."""

import hashlib
import json
import re
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID
from sqlalchemy import Table, select, func, Text, Select
from sqlalchemy.ext.asyncio import AsyncSession
# CLI/worker processes do not import API routers. Register retained FK targets
# explicitly before resolving archived tables' SQLAlchemy foreign-key columns.
from src.models import organization, workflow_definition  # noqa: F401
from src.models.conversation import Conversation
from src.models.message import Message
from src.models.workflow_run import WorkflowRun, WorkflowNodeRun
from src.models.interaction import (
    InteractionTurn,
    InteractionExecution,
    ToolOperation,
    UserQuestion,
    InteractionOutbox,
)
from src.models.web_source import WebSource
from src.models.knowledge_base import KnowledgeBase
from src.models.skill import Skill
from src.models.file_metadata import FileMetadata
from src.models.audit_log import AuditLog
from src.models.data_identity import AuditLogIdentity
from src.services.archive.schemas import (
    MAX_PART,
    MAX_CELL,
    ROWS_PER_PART,
    ArchiveFormatError,
    canonical,
    JSON_NULL,
)
from src.services.archive.catalogue import COLUMNS

MODELS = [
    Conversation,
    Message,
    WorkflowRun,
    WorkflowNodeRun,
    InteractionTurn,
    InteractionExecution,
    ToolOperation,
    UserQuestion,
    InteractionOutbox,
    WebSource,
    KnowledgeBase,
    Skill,
    FileMetadata,
    AuditLog,
]
TABLES: dict[str, Table] = {m.__tablename__: cast(Table, m.__table__) for m in MODELS}
# Parent-first insertion order; reversed for exact deletion.
ORDER = [
    "conversations",
    "messages",
    "workflow_runs",
    "workflow_node_runs",
    "interaction_turns",
    "interaction_executions",
    "tool_operations",
    "user_questions",
    "interaction_outbox",
    "web_sources",
    "knowledge_bases",
    "skills",
    "file_metadata",
]


def row_json(row: Mapping[str, Any], table: str) -> str:
    def transform(value: Any) -> Any:
        if isinstance(value, (UUID, Decimal)):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: transform(item) for key, item in value.items()}
        if isinstance(value, list):
            return [transform(item) for item in value]
        return value

    encoded = dict(row)
    for name, kind, _ in COLUMNS[table]:
        value = encoded.get(name)
        if kind == "json" and value is not None:
            encoded[name] = canonical(None if value is JSON_NULL else value)
    return canonical(transform(encoded))


def scrub(table: str, row: dict[str, Any]) -> dict[str, Any]:
    if table == "web_sources":
        row["content"] = None
    if table == "tool_operations" and row["kind"] in ("web_search", "web_read"):
        row["result"] = None
    if table in ("workflow_runs", "workflow_node_runs"):
        row["internal_error"] = None
    if table == "workflow_node_runs" and row["diagnostic_expires_at"] is not None:
        # Diagnostics are temporary regardless of whether the sweep has run.
        row["output"] = None
    return row


def selection(
    table: str, org: UUID, conversation: UUID | None, audit_ids: list[UUID] | None = None
) -> Select[Any]:
    target = TABLES[table]
    if {column.name for column in target.columns} != {name for name, _, _ in COLUMNS[table]}:
        raise ArchiveFormatError("database_schema_changed")
    columns = [
        target.c[name].cast(Text).label(name) if kind == "json" else target.c[name]
        for name, kind, _ in COLUMNS[table]
    ]
    query = select(*columns).where(target.c.org_id == org)
    if table == "audit_logs":
        return query.where(target.c.id.in_(audit_ids or []))
    if table == "conversations":
        return query.where(target.c.id == conversation)
    if "conversation_id" in target.c:
        return query.where(target.c.conversation_id == conversation)
    if "owning_conversation_id" in target.c:
        return query.where(target.c.owning_conversation_id == conversation)
    if table == "workflow_node_runs":
        return query.where(
            target.c.workflow_run_id.in_(
                select(WorkflowRun.id).where(
                    WorkflowRun.org_id == org, WorkflowRun.conversation_id == conversation
                )
            )
        )
    return query.where(
        target.c.turn_id.in_(
            select(InteractionTurn.id).where(
                InteractionTurn.org_id == org, InteractionTurn.conversation_id == conversation
            )
        )
    )


def decode_record(table: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(raw)
    for name, kind, _ in COLUMNS[table]:
        if kind == "inet" and row[name] is not None:
            row[name] = str(row[name])
        if kind == "json" and row[name] is not None:
            value = json.loads(row[name])
            row[name] = JSON_NULL if value is None else value
    return row


async def pages(
    session: AsyncSession,
    table: str,
    org: UUID,
    conversation: UUID | None,
    audit_ids: list[UUID] | None = None,
) -> AsyncIterator[list[dict[str, Any]]]:
    query = selection(table, org, conversation, audit_ids)
    # Return only aggregate sizes before transferring payload. Bound each cell
    # independently, then adapt row count to a fixed byte budget per SQL page.
    projected = query.subquery()
    sizes = (
        await session.execute(
            select(
                func.max(
                    func.greatest(
                        *(
                            func.coalesce(func.octet_length(column.cast(Text)), 0)
                            for column in projected.c
                        )
                    )
                ),
                func.max(func.octet_length(func.row_to_json(projected.table_valued()).cast(Text))),
            )
        )
    ).one()
    cell_bytes, row_bytes = sizes[0] or 0, sizes[1] or 0
    if cell_bytes > MAX_CELL:
        raise ArchiveFormatError("cell_too_large")
    if row_bytes > MAX_PART // 2:
        raise ArchiveFormatError("legacy_row_too_large")
    page_rows = min(ROWS_PER_PART, max(1, (MAX_PART // 2) // max(1, row_bytes)))
    offset = 0
    while True:
        rows = (
            (
                await session.execute(
                    query.order_by(TABLES[table].c.id).offset(offset).limit(page_rows)
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            return
        bounded: list[dict[str, Any]] = []
        size = 0
        for raw in rows:
            row = decode_record(table, dict(raw))
            row = scrub(table, row)
            width = len(row_json(row, table).encode())
            if bounded and size + width > MAX_PART // 2:
                yield bounded
                bounded = []
                size = 0
            bounded.append(row)
            size += width
        if bounded:
            yield bounded
        offset += len(rows)


async def inventory(
    session: AsyncSession, org: UUID, conversation: UUID | None, audit_ids: list[UUID] | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    kb_id = (
        await session.scalar(
            select(KnowledgeBase.id).where(
                KnowledgeBase.org_id == org, KnowledgeBase.owning_conversation_id == conversation
            )
        )
        if conversation
        else None
    )
    for table in ORDER if conversation else ["audit_logs"]:
        digest = hashlib.sha256()
        count = 0
        async for rows in pages(session, table, org, conversation, audit_ids):
            for row in rows:
                if table == "messages" and row["content_ref"] is not None:
                    raise ArchiveFormatError("external_message_reference_unsupported")
                if table == "file_metadata":
                    if (
                        kb_id is None
                        or row["document_id"] is None
                        or not re.fullmatch(
                            rf"documents/{org}/{kb_id}/[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}(?:\.[a-zA-Z0-9]{{1,16}})?",
                            row["storage_key"],
                        )
                    ):
                        raise ArchiveFormatError("attachment_inventory_unknown")
                if table == "skills":
                    schema = row["input_schema"]
                    expected_schema = {
                        "type": "object",
                        "properties": {
                            "knowledge_base_id": {"type": "string", "const": str(kb_id)},
                            "query": {"type": "string"},
                            "limit": {"type": "integer"},
                        },
                        "required": ["knowledge_base_id", "query"],
                    }
                    if (
                        row["type"] != "tool"
                        or row["activity_name"] != "execute_hybrid_search"
                        or any(
                            row[key] not in (None, JSON_NULL)
                            for key in (
                                "mcp_transport",
                                "mcp_endpoint",
                                "mcp_auth_token",
                                "workflow_type",
                                "bundle_storage_key",
                                "bundle_checksum",
                                "bundle_size_bytes",
                                "bundle_skill_name",
                                "allowed_child_skills",
                            )
                        )
                        or kb_id is None
                        or schema != expected_schema
                        or row["network_policy"] != "none"
                    ):
                        raise ArchiveFormatError("attachment_skill_shape_unknown")
                digest.update(row_json(row, table).encode() + b"\n")
                count += 1
        result[table] = {"count": count, "sha256": digest.hexdigest()}
    if audit_ids is not None:
        result["_audit_ids"] = [str(value) for value in audit_ids]
        identities = await session.execute(
            select(AuditLogIdentity.id, AuditLogIdentity.generation).where(
                AuditLogIdentity.org_id == org, AuditLogIdentity.id.in_(audit_ids)
            )
        )
        result["_audit_generations"] = {str(row.id): row.generation for row in identities}
    return result
