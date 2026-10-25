"""Hermes Agent capability.

Revision ID: 20261025_hermes_capability
Revises: 20261025_archive_images
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261025_hermes_capability"
down_revision: str | None = "20261025_archive_images"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_CAPABILITIES = (
    "'admin.users.read', 'admin.users.write', 'chat.send', 'images.generate', "
    "'sharing.manage', 'web.access', 'workspace.agents.create', "
    "'workspace.knowledge.create', 'workspace.skills.create'"
)
NEW_CAPABILITIES = (
    "'admin.users.read', 'admin.users.write', 'agents.hermes', 'chat.send', "
    "'images.generate', 'sharing.manage', 'web.access', 'workspace.agents.create', "
    "'workspace.knowledge.create', 'workspace.skills.create'"
)
CAPABILITY_CONSTRAINTS = (
    ("organization_capabilities", "ck_organization_capabilities_capability"),
    ("group_capabilities", "ck_group_capabilities_capability"),
)


def _replace(table: str, name: str, condition: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, condition)


def upgrade() -> None:
    for table, name in CAPABILITY_CONSTRAINTS:
        _replace(table, name, f"capability IN ({NEW_CAPABILITIES})")


def downgrade() -> None:
    # Fails instead of deleting data while Hermes grants exist.
    for table, name in CAPABILITY_CONSTRAINTS:
        _replace(table, name, f"capability IN ({OLD_CAPABILITIES})")
