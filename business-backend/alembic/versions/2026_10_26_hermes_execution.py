"""The execution a Hermes session's turn binding acts for.

Platform tools over the MCP bridge are durable operations of an interaction
execution; the binding names it, and the key keeps it inside the bound turn.

Revision ID: 20261026_hermes_execution
Revises: 20261025_hermes_egress
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261026_hermes_execution"
down_revision: str | None = "20261025_hermes_egress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE hermes_sessions ADD COLUMN active_execution_id uuid")
    # No caller binds turns before the chat agent; any binding made without an
    # execution is dropped, and the runner binds again.
    op.execute(
        "UPDATE hermes_sessions SET active_turn_id = NULL, workflow_run_id = NULL, "
        "workflow_phase = NULL, workflow_node_key = NULL WHERE active_turn_id IS NOT NULL"
    )
    op.execute(
        "ALTER TABLE hermes_sessions ADD CONSTRAINT fk_hermes_session_execution "
        "FOREIGN KEY (org_id, active_turn_id, active_execution_id) "
        "REFERENCES interaction_executions(org_id, turn_id, id)"
    )
    op.execute(
        "ALTER TABLE hermes_sessions ADD CONSTRAINT ck_hermes_session_execution "
        "CHECK ((active_turn_id IS NULL) = (active_execution_id IS NULL))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE hermes_sessions DROP CONSTRAINT ck_hermes_session_execution")
    op.execute("ALTER TABLE hermes_sessions DROP CONSTRAINT fk_hermes_session_execution")
    op.execute("ALTER TABLE hermes_sessions DROP COLUMN active_execution_id")
