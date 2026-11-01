"""Built-in capabilities of an agent definition.

Revision ID: 20261107_agent_capabilities
Revises: 20261106_agent_archive

An agent chooses whether a chat turn on it may search and read the web, generate
and edit images, and ask the user questions. All three default to on: existing
agents keep working, and the platform's switches and the user's capabilities
still decide what is actually offered.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261107_agent_capabilities"
down_revision: str | None = "20261106_agent_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("web_enabled", "images_enabled", "ask_user_enabled")


def upgrade() -> None:
    for column in COLUMNS:
        op.execute(f"ALTER TABLE agent_definitions ADD COLUMN {column} boolean NOT NULL DEFAULT true")


def downgrade() -> None:
    for column in reversed(COLUMNS):
        op.execute(f"ALTER TABLE agent_definitions DROP COLUMN {column}")
