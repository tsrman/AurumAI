"""remove legacy authorization schema

Revision ID: 2026_09_01_drop_legacy_auth
Revises: 2026_09_01_agent_definitions
Create Date: 2026-09-01 14:00:00.000000

The downgrade is intentionally lossy: it recreates the removed structures,
policies, indexes, and grants, but cannot reconstruct deleted permission or
backfill-marker data.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "2026_09_01_drop_legacy_auth"
down_revision: str | None = "2026_09_01_agent_definitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("group_permissions")
    op.drop_column("skills", "permissions")
    op.drop_table("authorization_migration_runs")


def downgrade() -> None:
    op.create_table(
        "authorization_migration_runs",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("identity_digest", sa.String(length=64), nullable=False),
        sa.Column("catalog_digest", sa.String(length=64), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status = 'complete'",
            name="ck_authorization_migration_runs_status",
        ),
        sa.PrimaryKeyConstraint("org_id", "version"),
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON authorization_migration_runs "
        "TO aurum_ai_app"
    )
    op.execute("ALTER TABLE authorization_migration_runs ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY authorization_migration_runs_org_isolation
        ON authorization_migration_runs
        USING (org_id::text = current_setting('app.current_org_id', true))
        WITH CHECK (org_id::text = current_setting('app.current_org_id', true))
        """
    )
    op.execute("ALTER TABLE authorization_migration_runs FORCE ROW LEVEL SECURITY")

    op.add_column("skills", sa.Column("permissions", sa.JSON(), nullable=True))

    op.create_table(
        "group_permissions",
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("permission", sa.String(length=255), nullable=False),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
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
        op.f("ix_group_permissions_group_id"),
        "group_permissions",
        ["group_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_group_permissions_permission"),
        "group_permissions",
        ["permission"],
        unique=False,
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON group_permissions TO aurum_ai_app"
    )
    op.execute("ALTER TABLE group_permissions ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY group_permissions_org_isolation ON group_permissions
        USING (
            EXISTS (
                SELECT 1 FROM groups
                WHERE groups.id = group_permissions.group_id
                AND groups.org_id::text = current_setting('app.current_org_id', true)
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM groups
                WHERE groups.id = group_permissions.group_id
                AND groups.org_id::text = current_setting('app.current_org_id', true)
            )
        )
        """
    )
    op.execute("ALTER TABLE group_permissions FORCE ROW LEVEL SECURITY")
