"""add authorization v2 capabilities and resource grants

Revision ID: 2026_08_31_authorization_v2
Revises: 2026_08_31_reserved_sweep
Create Date: 2026-08-31 23:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2026_08_31_authorization_v2"
down_revision: Union[str, None] = "2026_08_31_reserved_sweep"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CAPABILITY_VALUES = (
    "'admin.users.read', 'admin.users.write', 'chat.send', 'sharing.manage', "
    "'workspace.agents.create', 'workspace.knowledge.create', "
    "'workspace.skills.create'"
)


def _enable_direct_org_rls(table: str, policy: str) -> None:
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO aurum_ai_app")
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {policy} ON {table}
        USING (org_id::text = current_setting('app.current_org_id', true))
        WITH CHECK (org_id::text = current_setting('app.current_org_id', true))
        """
    )
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    op.create_table(
        "organization_capabilities",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("capability", sa.String(length=100), nullable=False),
        sa.CheckConstraint(
            f"capability IN ({CAPABILITY_VALUES})",
            name="ck_organization_capabilities_capability",
        ),
        sa.PrimaryKeyConstraint("org_id", "capability"),
    )
    op.create_table(
        "group_capabilities",
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("capability", sa.String(length=100), nullable=False),
        sa.CheckConstraint(
            f"capability IN ({CAPABILITY_VALUES})",
            name="ck_group_capabilities_capability",
        ),
        sa.PrimaryKeyConstraint("group_id", "capability"),
    )
    op.create_table(
        "access_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=False),
        sa.Column("principal_type", sa.String(length=32), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission", sa.String(length=16), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_via",
            sa.String(length=20),
            server_default=sa.text("'admin'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "resource_type IN ('model', 'skill', 'knowledge_base', 'agent_definition')",
            name="ck_access_grants_resource_type",
        ),
        sa.CheckConstraint(
            "principal_type IN ('organization', 'group', 'user')",
            name="ck_access_grants_principal_type",
        ),
        sa.CheckConstraint(
            "permission IN ('read', 'write')",
            name="ck_access_grants_permission",
        ),
        sa.CheckConstraint(
            "created_via IN ('admin', 'migration', 'provisioning')",
            name="ck_access_grants_created_via",
        ),
        sa.CheckConstraint(
            "((created_via = 'admin' AND created_by IS NOT NULL) OR "
            "(created_via IN ('migration', 'provisioning') AND created_by IS NULL))",
            name="ck_access_grants_source_actor",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "resource_type",
            "resource_id",
            "principal_type",
            "principal_id",
            "permission",
            name="uq_access_grants_resource_principal_permission",
        ),
    )
    op.create_index(
        "ix_access_grants_resource_lookup",
        "access_grants",
        ["org_id", "resource_type", "resource_id", "permission"],
    )
    op.create_index(
        "ix_access_grants_principal_lookup",
        "access_grants",
        ["org_id", "principal_type", "principal_id", "permission"],
    )
    op.create_table(
        "authorization_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=False),
        sa.Column("before_state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after_state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("correlation_id", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_authorization_audit_events_resource",
        "authorization_audit_events",
        ["org_id", "resource_type", "resource_id", "created_at"],
    )
    op.create_index(
        "ix_authorization_audit_events_actor",
        "authorization_audit_events",
        ["org_id", "actor_id", "created_at"],
    )

    _enable_direct_org_rls(
        "organization_capabilities", "organization_capabilities_org_isolation"
    )

    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON group_capabilities TO aurum_ai_app"
    )
    op.execute("ALTER TABLE group_capabilities ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY group_capabilities_org_isolation ON group_capabilities
        USING (
            EXISTS (
                SELECT 1 FROM groups
                WHERE groups.id = group_capabilities.group_id
                  AND groups.org_id::text = current_setting('app.current_org_id', true)
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM groups
                WHERE groups.id = group_capabilities.group_id
                  AND groups.org_id::text = current_setting('app.current_org_id', true)
            )
        )
        """
    )
    op.execute("ALTER TABLE group_capabilities FORCE ROW LEVEL SECURITY")

    _enable_direct_org_rls("access_grants", "access_grants_org_isolation")
    _enable_direct_org_rls(
        "authorization_audit_events", "authorization_audit_events_org_isolation"
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS authorization_audit_events_org_isolation "
        "ON authorization_audit_events"
    )
    op.execute("DROP POLICY IF EXISTS access_grants_org_isolation ON access_grants")
    op.execute(
        "DROP POLICY IF EXISTS group_capabilities_org_isolation ON group_capabilities"
    )
    op.execute(
        "DROP POLICY IF EXISTS organization_capabilities_org_isolation "
        "ON organization_capabilities"
    )
    op.drop_index("ix_access_grants_principal_lookup", table_name="access_grants")
    op.drop_index("ix_access_grants_resource_lookup", table_name="access_grants")
    op.drop_table("access_grants")
    op.drop_index(
        "ix_authorization_audit_events_actor",
        table_name="authorization_audit_events",
        if_exists=True,
    )
    op.drop_index(
        "ix_authorization_audit_events_resource",
        table_name="authorization_audit_events",
        if_exists=True,
    )
    op.drop_table("authorization_audit_events", if_exists=True)
    op.drop_table("group_capabilities")
    op.drop_table("organization_capabilities")
