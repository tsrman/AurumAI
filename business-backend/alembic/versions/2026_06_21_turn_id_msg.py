"""add turn_id to messages

Revision ID: 2026_06_21_turn_id_msg
Revises: 2026_06_21_app_role_force_rls
Create Date: 2026-06-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2026_06_21_turn_id_msg"
down_revision: Union[str, None] = "2026_06_21_app_role_force_rls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("turn_id", sa.UUID(as_uuid=True), nullable=True))
    op.create_index("idx_messages_turn_id", "messages", ["conversation_id", "turn_id"])


def downgrade() -> None:
    op.drop_index("idx_messages_turn_id", table_name="messages")
    op.drop_column("messages", "turn_id")
