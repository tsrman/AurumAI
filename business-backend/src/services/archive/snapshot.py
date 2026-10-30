"""Versioned, bounded FK graph inventory. Ordinary definitions and grants stay live."""

import hashlib
import json
import re
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID
from sqlalchemy import Table, select, func, Text, Select, exists, or_
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
from src.models.conversation_file import ConversationFile
from src.models.conversation_image import ConversationImage
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
from src.services.archive.catalogue import CURRENT_VERSION, OMITTED_DEFAULTS, catalogue

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
    ConversationImage,
    ConversationFile,
    KnowledgeBase,
    Skill,
    FileMetadata,
    AuditLog,
]
TABLES: dict[str, Table] = {m.__tablename__: cast(Table, m.__table__) for m in MODELS}
# Parent-first insertion order; reversed for exact deletion.
ORDER_V1 = [
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
# Images follow their tool operations; a self-reference to the edited image
# is restored after every image row exists.
ORDER_V2 = [
    *ORDER_V1[: ORDER_V1.index("web_sources") + 1],
    "conversation_images",
    *ORDER_V1[ORDER_V1.index("web_sources") + 1 :],
]
# Files follow their tool operations, after the images.
ORDER_V3 = [
    *ORDER_V2[: ORDER_V2.index("conversation_images") + 1],
    "conversation_files",
    *ORDER_V2[ORDER_V2.index("conversation_images") + 1 :],
]
ORDERS = {1: ORDER_V1, 2: ORDER_V2, 3: ORDER_V3}
ORDER = ORDERS[CURRENT_VERSION]
VERSION_KEY = "_archive_version"
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def inventory_version(frozen: Mapping[str, Any]) -> int:
    """Jobs frozen before versioning carry no marker and are v1."""
    value = frozen.get(VERSION_KEY, 1)
    if value not in ORDERS:
        raise ArchiveFormatError("archive_version_unknown")
    return int(value)


def conversation_tables(version: int) -> list[str]:
    return ORDERS[version]


def row_json(row: Mapping[str, Any], table: str, version: int = CURRENT_VERSION) -> str:
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
    for name, kind, _ in catalogue(version)[table]:
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
    table: str,
    org: UUID,
    conversation: UUID | None,
    audit_ids: list[UUID] | None = None,
    version: int = CURRENT_VERSION,
) -> Select[Any]:
    target = TABLES[table]
    columns_of_version = catalogue(version)[table]
    omitted = set(OMITTED_DEFAULTS.get(version, {}).get(table, {}))
    if {column.name for column in target.columns} != {
        name for name, _, _ in columns_of_version
    } | omitted:
        raise ArchiveFormatError("database_schema_changed")
    columns = [
        target.c[name].cast(Text).label(name) if kind == "json" else target.c[name]
        for name, kind, _ in columns_of_version
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


def decode_record(
    table: str, raw: Mapping[str, Any], version: int = CURRENT_VERSION
) -> dict[str, Any]:
    row = dict(raw)
    for name, kind, _ in catalogue(version)[table]:
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
    version: int = CURRENT_VERSION,
) -> AsyncIterator[list[dict[str, Any]]]:
    query = selection(table, org, conversation, audit_ids, version)
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
            row = decode_record(table, dict(raw), version)
            row = scrub(table, row)
            width = len(row_json(row, table, version).encode())
            if bounded and size + width > MAX_PART // 2:
                yield bounded
                bounded = []
                size = 0
            bounded.append(row)
            size += width
        if bounded:
            yield bounded
        offset += len(rows)


async def _omitted_columns_hold_defaults(
    session: AsyncSession, table: str, org: UUID, conversation: UUID | None, version: int
) -> None:
    """An older-version archive cannot carry a column it omits, so the live
    value must still be the default a restore reinstates."""
    defaults = OMITTED_DEFAULTS.get(version, {}).get(table, {})
    if not defaults:
        return
    target = TABLES[table]
    query = selection(table, org, conversation, version=version).where(
        or_(*(target.c[name].is_distinct_from(value) for name, value in defaults.items()))
    )
    if await session.scalar(select(exists(query))):
        raise ArchiveFormatError("archive_version_incompatible")


async def _later_tables_are_empty(
    session: AsyncSession, org: UUID, conversation: UUID, version: int
) -> None:
    """An older-version archive cannot carry a table added after it, and the
    purge would cascade over its rows: they must not exist."""
    for table in ORDER:
        if table in ORDERS[version]:
            continue
        if await session.scalar(select(exists(selection(table, org, conversation)))):
            raise ArchiveFormatError("archive_version_incompatible")


def check_image_key(row: Mapping[str, Any], org: UUID, conversation: UUID | None) -> None:
    # Bytes are copied by storage key, so the key must be this conversation's
    # own and name the row that owns it: the operation, or the upload itself.
    match = re.fullmatch(
        rf"conversation-images/{org}/{conversation}/(uploads/)?({_UUID})\.(png|jpg|webp)",
        row["storage_key"],
    )
    owner = row["id"] if match and match[1] else row["operation_id"]
    if match is None or owner is None or match[2] != str(owner):
        raise ArchiveFormatError("image_inventory_unknown")


def check_file_key(row: Mapping[str, Any], org: UUID, conversation: UUID | None) -> None:
    # As for images: this conversation's files folder, named by the operation.
    match = re.fullmatch(
        rf"conversation-images/{org}/{conversation}/files/({_UUID})\.(csv|txt|md|json|pdf|xlsx|docx|pptx|zip)",
        row["storage_key"],
    )
    if match is None or row["operation_id"] is None or match[1] != str(row["operation_id"]):
        raise ArchiveFormatError("file_inventory_unknown")


async def inventory(
    session: AsyncSession,
    org: UUID,
    conversation: UUID | None,
    audit_ids: list[UUID] | None = None,
    version: int = CURRENT_VERSION,
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
    if conversation:
        await _later_tables_are_empty(session, org, conversation, version)
    for table in conversation_tables(version) if conversation else ["audit_logs"]:
        digest = hashlib.sha256()
        count = 0
        if conversation:
            await _omitted_columns_hold_defaults(session, table, org, conversation, version)
        async for rows in pages(session, table, org, conversation, audit_ids, version):
            for row in rows:
                if table == "conversation_images":
                    check_image_key(row, org, conversation)
                if table == "conversation_files":
                    check_file_key(row, org, conversation)
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
                digest.update(row_json(row, table, version).encode() + b"\n")
                count += 1
        result[table] = {"count": count, "sha256": digest.hexdigest()}
    if conversation and version > 1:
        result[VERSION_KEY] = version
    if audit_ids is not None:
        result["_audit_ids"] = [str(value) for value in audit_ids]
        identities = await session.execute(
            select(AuditLogIdentity.id, AuditLogIdentity.generation).where(
                AuditLogIdentity.org_id == org, AuditLogIdentity.id.in_(audit_ids)
            )
        )
        result["_audit_generations"] = {str(row.id): row.generation for row in identities}
    return result
