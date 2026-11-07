"""Where a group membership comes from.

Revision ID: 20261109_group_member_source
Revises: 20261108_hermes_agent_settings

Sign-in synchronization replaces a user's memberships with the Keycloak `groups`
claim. An administrator can now also add people to a group in the Admin UI, and
those memberships must survive the next sign-in. `identity` rows are owned by the
synchronization (every existing row is one); `manual` rows are owned by the
administrators and are never touched by it.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261109_group_member_source"
down_revision: str | None = "20261108_hermes_agent_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_groups ADD COLUMN source varchar(16) NOT NULL DEFAULT 'identity'")
    op.execute(
        "ALTER TABLE user_groups ADD CONSTRAINT ck_user_groups_source "
        "CHECK (source IN ('identity', 'manual'))"
    )


def downgrade() -> None:
    # Manual memberships stay as plain memberships; only their origin is forgotten.
    op.execute("ALTER TABLE user_groups DROP CONSTRAINT ck_user_groups_source")
    op.execute("ALTER TABLE user_groups DROP COLUMN source")
