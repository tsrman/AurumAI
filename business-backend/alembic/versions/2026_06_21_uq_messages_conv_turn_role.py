"""add unique constraint on messages (conversation_id, turn_id, role)

Revision ID: 2026_06_21_uq_msg_turn_role
Revises: 2026_06_21_turn_id_msg
Create Date: 2026-06-21 04:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2026_06_21_uq_msg_turn_role"
down_revision: Union[str, None] = "2026_06_21_turn_id_msg"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_messages_conv_turn_role",
        "messages",
        ["conversation_id", "turn_id", "role"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_messages_conv_turn_role", "messages", type_="unique")
