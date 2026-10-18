"""add monthly LLM usage accounting

Revision ID: 2026_08_31_llm_usage
Revises: 2026_08_29_file_metadata
Create Date: 2026-08-31 16:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2026_08_31_llm_usage"
down_revision: Union[str, None] = "2026_08_29_file_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enable_org_rls(table: str, policy: str) -> None:
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
        "organization_usage_monthly",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("period_start", sa.Date(), primary_key=True),
        sa.Column("requests_used", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("total_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "requests_used >= 0", name="ck_organization_usage_monthly_requests_used"
        ),
        sa.CheckConstraint(
            "prompt_tokens >= 0", name="ck_organization_usage_monthly_prompt_tokens"
        ),
        sa.CheckConstraint(
            "completion_tokens >= 0",
            name="ck_organization_usage_monthly_completion_tokens",
        ),
        sa.CheckConstraint(
            "total_tokens >= 0", name="ck_organization_usage_monthly_total_tokens"
        ),
    )

    op.create_table(
        "llm_usage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="reserved", nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("total_tokens", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('reserved', 'accepted', 'completed')",
            name="ck_llm_usage_events_status",
        ),
        sa.CheckConstraint("prompt_tokens >= 0", name="ck_llm_usage_events_prompt_tokens"),
        sa.CheckConstraint(
            "completion_tokens >= 0", name="ck_llm_usage_events_completion_tokens"
        ),
        sa.CheckConstraint("total_tokens >= 0", name="ck_llm_usage_events_total_tokens"),
        sa.UniqueConstraint("request_id", name="uq_llm_usage_events_request_id"),
    )
    op.create_index("ix_llm_usage_events_org_id", "llm_usage_events", ["org_id"])
    op.create_index("ix_llm_usage_events_user_id", "llm_usage_events", ["user_id"])
    op.create_index(
        "ix_llm_usage_events_conversation_id", "llm_usage_events", ["conversation_id"]
    )
    op.create_index("ix_llm_usage_events_period_start", "llm_usage_events", ["period_start"])

    _enable_org_rls(
        "organization_usage_monthly", "organization_usage_monthly_org_isolation"
    )
    _enable_org_rls("llm_usage_events", "llm_usage_events_org_isolation")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS llm_usage_events_org_isolation ON llm_usage_events")
    op.execute(
        "DROP POLICY IF EXISTS organization_usage_monthly_org_isolation "
        "ON organization_usage_monthly"
    )
    op.drop_index("ix_llm_usage_events_period_start", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_conversation_id", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_user_id", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_org_id", table_name="llm_usage_events")
    op.drop_table("llm_usage_events")
    op.drop_table("organization_usage_monthly")
