"""add audit_logs

Revision ID: 2026_08_29_audit_logs
Revises: 2026_07_19_org_rls
Create Date: 2026-08-29 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '2026_08_29_audit_logs'
down_revision: Union[str, None] = '2026_07_19_org_rls'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Audit log of mutating requests. The table is organization-scoped and uses
# the same tenant policy based on app.current_org_id
# plus FORCE ROW LEVEL SECURITY, preventing aurum_ai_app from reading foreign
# rows when application code accidentally omits the tenant context.
#
# aurum_ai_app is created by 2026_06_21_app_role_force_rls;
# grants on new tables are not inherited automatically. ALTER DEFAULT
# PRIVILEGES there applies only to future tables created by postgres,
# which is our case. The explicit grant below avoids depending on
# migration execution order.


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("path", sa.String(length=255), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_logs_org_id", "audit_logs", ["org_id"])
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])
    op.create_index("ix_audit_logs_path", "audit_logs", ["path"])
    # The usual audit query asks what happened recently in this organization,
    # so the composite index orders organization records by descending time.
    op.create_index(
        "ix_audit_logs_org_created_at",
        "audit_logs",
        ["org_id", sa.text("created_at DESC")],
    )

    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON audit_logs TO aurum_ai_app"
    )
    op.execute("ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY audit_logs_org_isolation ON audit_logs
        USING (org_id::text = current_setting('app.current_org_id', true))
        WITH CHECK (org_id::text = current_setting('app.current_org_id', true))
        """
    )
    op.execute("ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS audit_logs_org_isolation ON audit_logs")
    op.drop_index("ix_audit_logs_org_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_path", table_name="audit_logs")
    op.drop_index("ix_audit_logs_user_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_org_id", table_name="audit_logs")
    op.drop_table("audit_logs")
