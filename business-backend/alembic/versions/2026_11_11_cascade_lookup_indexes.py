"""Indexes for the lookups a conversation delete makes, and fewer duplicate org_id indexes.

Revision ID: 20261111_cascade_lookup_indexes
Revises: 20261110_conversation_list_index

Deleting a conversation checks every table that points at it. Five of them had no index
starting with the foreign key's columns, so each delete scanned them whole:
``interaction_turns``, ``conversation_executions``, ``hermes_sessions``, ``archive_jobs``
and ``file_metadata``. Each now has one.

Six tables also had an index on ``org_id`` alone next to another index that already starts
with ``org_id``. The extra one answers nothing the other cannot and only adds to every
insert and update on tables that are written constantly, so it is dropped.

``CREATE INDEX`` blocks writes to the table while it builds. Run the upgrade in a quiet
period on a large installation.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261111_cascade_lookup_indexes"
down_revision: str | None = "20261110_conversation_list_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CREATED = (
    "CREATE INDEX IF NOT EXISTS ix_interaction_turns_conversation ON interaction_turns (org_id, conversation_id)",
    "CREATE INDEX IF NOT EXISTS ix_conversation_executions_conversation ON conversation_executions (org_id, conversation_id)",
    "CREATE INDEX IF NOT EXISTS ix_hermes_sessions_conversation ON hermes_sessions (org_id, conversation_id)",
    "CREATE INDEX IF NOT EXISTS ix_archive_jobs_conversation ON archive_jobs (org_id, conversation_id)",
    "CREATE INDEX IF NOT EXISTS ix_file_metadata_conversation ON file_metadata (org_id, owning_conversation_id) "
    "WHERE owning_conversation_id IS NOT NULL",
)

# (index, table, columns): the single-column org_id indexes that are dropped.
_DROPPED = (
    ("ix_interaction_turns_org_id", "interaction_turns"),
    ("ix_conversation_executions_org_id", "conversation_executions"),
    ("ix_hermes_sessions_org_id", "hermes_sessions"),
    ("ix_tool_operations_org_id", "tool_operations"),
    ("ix_conversations_org_id_copy", "conversations"),
    ("ix_audit_logs_org_id_copy", "audit_logs"),
)

_CREATED_NAMES = (
    "ix_interaction_turns_conversation",
    "ix_conversation_executions_conversation",
    "ix_hermes_sessions_conversation",
    "ix_archive_jobs_conversation",
    "ix_file_metadata_conversation",
)


def upgrade() -> None:
    for statement in _CREATED:
        op.execute(statement)
    for index, _table in _DROPPED:
        op.execute(f"DROP INDEX IF EXISTS {index}")


def downgrade() -> None:
    for index, table in _DROPPED:
        op.execute(f"CREATE INDEX IF NOT EXISTS {index} ON {table} (org_id)")
    for name in _CREATED_NAMES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
