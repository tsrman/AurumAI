"""The model an organization's Hermes Agent uses.

Revision ID: 20261026_hermes_model
Revises: 20261026_hermes_execution
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20261026_hermes_model"
down_revision: str | None = "20261026_hermes_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("organization_settings", sa.Column("hermes_model", sa.String(255)))


def downgrade() -> None:
    op.drop_column("organization_settings", "hermes_model")
