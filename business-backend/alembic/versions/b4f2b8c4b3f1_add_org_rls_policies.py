"""add org rls policies

Revision ID: b4f2b8c4b3f1
Revises: 958b310beac2
Create Date: 2026-06-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = "b4f2b8c4b3f1"
down_revision: Union[str, None] = "958b310beac2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ORG_MATCH = "org_id::text = current_setting('app.current_org_id', true)"


def _org_match(column: str = "org_id") -> str:
    return f"{column}::text = current_setting('app.current_org_id', true)"


def _enable_org_policy(table: str) -> None:
    policy_name = f"{table}_org_isolation"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table}")
    op.execute(
        f"""
        CREATE POLICY {policy_name} ON {table}
        USING ({ORG_MATCH})
        WITH CHECK ({ORG_MATCH})
        """
    )


def upgrade() -> None:
    for table in ("users", "conversations", "messages", "groups"):
        _enable_org_policy(table)

    op.execute("ALTER TABLE user_settings ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS user_settings_org_isolation ON user_settings")
    op.execute(
        f"""
        CREATE POLICY user_settings_org_isolation ON user_settings
        USING (
            EXISTS (
                SELECT 1 FROM users
                WHERE users.id = user_settings.user_id AND {_org_match('users.org_id')}
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM users
                WHERE users.id = user_settings.user_id AND {_org_match('users.org_id')}
            )
        )
        """
    )

    op.execute("ALTER TABLE group_permissions ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS group_permissions_org_isolation ON group_permissions")
    op.execute(
        f"""
        CREATE POLICY group_permissions_org_isolation ON group_permissions
        USING (
            EXISTS (
                SELECT 1 FROM groups
                WHERE groups.id = group_permissions.group_id
                AND {_org_match('groups.org_id')}
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM groups
                WHERE groups.id = group_permissions.group_id
                AND {_org_match('groups.org_id')}
            )
        )
        """
    )

    op.execute("ALTER TABLE user_groups ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS user_groups_org_isolation ON user_groups")
    op.execute(
        f"""
        CREATE POLICY user_groups_org_isolation ON user_groups
        USING (
            EXISTS (
                SELECT 1 FROM users
                WHERE users.id = user_groups.user_id
                AND {_org_match('users.org_id')}
            )
            AND EXISTS (
                SELECT 1 FROM groups
                WHERE groups.id = user_groups.group_id
                AND {_org_match('groups.org_id')}
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM users
                WHERE users.id = user_groups.user_id
                AND {_org_match('users.org_id')}
            )
            AND EXISTS (
                SELECT 1 FROM groups
                WHERE groups.id = user_groups.group_id
                AND {_org_match('groups.org_id')}
            )
        )
        """
    )

    op.execute("ALTER TABLE skills ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS skills_org_isolation ON skills")
    op.execute(
        f"""
        CREATE POLICY skills_org_isolation ON skills
        USING (org_id IS NULL OR {ORG_MATCH})
        WITH CHECK (org_id IS NULL OR {ORG_MATCH})
        """
    )


def downgrade() -> None:
    for table in (
        "skills",
        "user_groups",
        "group_permissions",
        "user_settings",
        "groups",
        "messages",
        "conversations",
        "users",
    ):
        op.execute(f"DROP POLICY IF EXISTS {table}_org_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
