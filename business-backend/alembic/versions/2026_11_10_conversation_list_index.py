"""An index for the conversation list.

Revision ID: 20261110_conversation_list_index
Revises: 20261109_group_member_source

The sidebar lists one person's conversations, newest activity first. No index
started with ``(org_id, user_id)``, so every list read every partition of
``conversations`` and sorted what it found: the cost grew with all conversations
of the organization, not with the person's. This index answers the list from a
single ordered range per partition.

``CREATE INDEX`` on a partitioned table blocks writes to ``conversations`` while
it builds. Run the upgrade in a quiet period on a large installation.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261110_conversation_list_index"
down_revision: str | None = "20261109_group_member_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversations_user_activity ON conversations "
        "(org_id, user_id, (COALESCE(last_message_at, created_at)) DESC, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_conversations_user_activity")
