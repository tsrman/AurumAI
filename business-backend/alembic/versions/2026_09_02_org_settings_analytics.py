"""align organization model settings and usage analytics indexes

Revision ID: 2026_09_02_org_analytics
Revises: 2026_09_01_drop_legacy_auth
Create Date: 2026-09-02 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "2026_09_02_org_analytics"
down_revision: str | None = "2026_09_01_drop_legacy_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "organization_settings",
        "default_model",
        existing_type=sa.String(length=100),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.alter_column(
        "user_settings",
        "default_model",
        existing_type=sa.String(length=100),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.create_index(
        "ix_llm_usage_events_org_accepted_at",
        "llm_usage_events",
        ["org_id", "accepted_at"],
        unique=False,
        postgresql_where=sa.text("status = 'accepted'"),
    )
    op.create_index(
        "ix_llm_usage_events_org_completed_at",
        "llm_usage_events",
        ["org_id", "completed_at"],
        unique=False,
        postgresql_where=sa.text("status = 'completed'"),
    )


def downgrade() -> None:
    has_oversized_model_id = op.get_bind().execute(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM organization_settings "
            "WHERE char_length(default_model) > 100 "
            "UNION ALL "
            "SELECT 1 FROM user_settings "
            "WHERE char_length(default_model) > 100"
            ")"
        )
    ).scalar_one()
    if has_oversized_model_id:
        raise RuntimeError(
            "Cannot downgrade organization model settings: a persisted "
            "default_model exceeds the legacy 100-character limit"
        )

    op.drop_index(
        "ix_llm_usage_events_org_completed_at",
        table_name="llm_usage_events",
    )
    op.drop_index(
        "ix_llm_usage_events_org_accepted_at",
        table_name="llm_usage_events",
    )
    op.alter_column(
        "user_settings",
        "default_model",
        existing_type=sa.String(length=255),
        type_=sa.String(length=100),
        existing_nullable=True,
    )
    op.alter_column(
        "organization_settings",
        "default_model",
        existing_type=sa.String(length=255),
        type_=sa.String(length=100),
        existing_nullable=True,
    )
