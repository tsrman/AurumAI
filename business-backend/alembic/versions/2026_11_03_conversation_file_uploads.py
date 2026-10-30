"""Files the user uploads for Hermes Agent's workspace.

Revision ID: 20261103_file_uploads
Revises: 20261102_conversation_files

As conversation images: an upload carries no tool operation and its user
message claims it by setting the turn. Its bytes live in the conversation's
files/uploads/ folder, named after the row itself.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20261103_file_uploads"
down_revision: str | None = "20261102_conversation_files"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "conversation_files"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("origin", sa.Text(), nullable=False, server_default="generated"))
    for column in ("turn_id", "execution_id", "operation_id"):
        op.alter_column(TABLE, column, nullable=True)
    op.create_check_constraint(
        "ck_conversation_file_origin", TABLE, "origin IN ('generated','uploaded')"
    )
    # An agent's file names its operation; an upload never has one.
    op.create_check_constraint(
        "ck_conversation_file_origin_operation",
        TABLE,
        "(origin = 'generated' AND turn_id IS NOT NULL AND execution_id IS NOT NULL "
        "AND operation_id IS NOT NULL) OR (origin = 'uploaded' AND execution_id IS NULL "
        "AND operation_id IS NULL)",
    )


def downgrade() -> None:
    # Fails instead of deleting uploads while any exist.
    op.drop_constraint("ck_conversation_file_origin_operation", TABLE, type_="check")
    op.drop_constraint("ck_conversation_file_origin", TABLE, type_="check")
    for column in ("turn_id", "execution_id", "operation_id"):
        op.alter_column(TABLE, column, nullable=False)
    op.drop_column(TABLE, "origin")
