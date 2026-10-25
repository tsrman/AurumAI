"""Usage events record what kind of provider call they count.

Revision ID: 20261025_usage_kind
Revises: 20261025_vision_models
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261025_usage_kind"
down_revision: str | None = "20261025_vision_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows are chat completions, the only calls counted until now.
    op.add_column(
        "llm_usage_events",
        sa.Column("kind", sa.String(32), server_default="chat", nullable=False),
    )
    op.create_check_constraint(
        "ck_llm_usage_events_kind",
        "llm_usage_events",
        "kind IN ('chat', 'image_generate', 'image_edit')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_llm_usage_events_kind", "llm_usage_events", type_="check")
    op.drop_column("llm_usage_events", "kind")
