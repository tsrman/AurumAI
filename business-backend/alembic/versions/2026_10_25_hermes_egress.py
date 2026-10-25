"""Hermes Agent internet egress mode per organization.

Revision ID: 20261025_hermes_egress
Revises: 20261025_hermes_sessions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20261025_hermes_egress"
down_revision: str | None = "20261025_hermes_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("organization_settings", sa.Column("hermes_egress_mode", sa.Text()))
    op.create_check_constraint(
        "ck_organization_settings_hermes_egress_mode",
        "organization_settings",
        "hermes_egress_mode IS NULL OR hermes_egress_mode IN ('none','packages','public')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_organization_settings_hermes_egress_mode", "organization_settings", type_="check"
    )
    op.drop_column("organization_settings", "hermes_egress_mode")
