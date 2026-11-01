"""Archiving of agent definitions.

Revision ID: 20261106_agent_archive
Revises: 20261105_hermes_previews

An agent that a workflow or a run ever referred to cannot be deleted (the
foreign keys keep the history intact), so it is archived instead: hidden from
lists and pickers, refused for new use, history untouched.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261106_agent_archive"
down_revision: str | None = "20261105_hermes_previews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_definitions ADD COLUMN archived_at timestamptz")
    op.execute("ALTER TABLE agent_definitions ADD COLUMN archived_by uuid REFERENCES users(id) ON DELETE SET NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_definitions DROP COLUMN archived_by")
    op.execute("ALTER TABLE agent_definitions DROP COLUMN archived_at")
