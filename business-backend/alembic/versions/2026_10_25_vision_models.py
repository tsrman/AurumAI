"""Per-organization list of chat models that accept image input.

Revision ID: 20261025_vision_models
Revises: 20261024_image_tools
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261025_vision_models"
down_revision: str | None = "20261024_image_tools"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organization_settings",
        sa.Column(
            "vision_models",
            postgresql.ARRAY(sa.String(255)),
            server_default="{}",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_organization_settings_vision_models",
        "organization_settings",
        "cardinality(vision_models) <= 100",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_organization_settings_vision_models", "organization_settings", type_="check"
    )
    op.drop_column("organization_settings", "vision_models")
