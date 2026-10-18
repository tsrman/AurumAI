"""Add web.access to canonical capability constraints.

Revision ID: d7f9b2c5a1e4
Revises: c6e8a1b4d9f2
"""

from collections.abc import Sequence

from alembic import op


revision: str = "d7f9b2c5a1e4"
down_revision: str | None = "c6e8a1b4d9f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_VALUES = (
    "'admin.users.read', 'admin.users.write', 'chat.send', 'sharing.manage', "
    "'workspace.agents.create', 'workspace.knowledge.create', "
    "'workspace.skills.create'"
)
NEW_VALUES = (
    "'admin.users.read', 'admin.users.write', 'chat.send', 'sharing.manage', 'web.access', "
    "'workspace.agents.create', 'workspace.knowledge.create', "
    "'workspace.skills.create'"
)
CONSTRAINTS = (
    ("organization_capabilities", "ck_organization_capabilities_capability"),
    ("group_capabilities", "ck_group_capabilities_capability"),
)


def _replace_constraints(values: str) -> None:
    for table, name in CONSTRAINTS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"capability IN ({values})")


def upgrade() -> None:
    _replace_constraints(NEW_VALUES)


def downgrade() -> None:
    # A downgrade with active web grants must fail instead of silently
    # deleting authorization data.
    _replace_constraints(OLD_VALUES)
