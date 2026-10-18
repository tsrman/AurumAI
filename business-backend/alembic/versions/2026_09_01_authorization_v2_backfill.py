"""add authorization v2 backfill completion markers

Revision ID: 2026_09_01_auth_v2_backfill
Revises: 2026_08_31_authorization_v2
Create Date: 2026-09-01 01:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2026_09_01_auth_v2_backfill"
down_revision: Union[str, None] = "2026_08_31_authorization_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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
            "status = 'complete'", name="ck_authorization_migration_runs_status"
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


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS authorization_migration_runs_org_isolation "
        "ON authorization_migration_runs"
    )
    op.drop_table("authorization_migration_runs")
