"""add temperature settings at org, agent and conversation level

Revision ID: 78b705d3b523
Revises: c2e5a8b1d4f7
Create Date: 2026-09-12 17:00:09.790182

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '78b705d3b523'
down_revision: Union[str, None] = 'c2e5a8b1d4f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No DB-level CHECK on these two, matching each table's own existing
    # convention (organization_settings/conversations validate bounds only at
    # the Pydantic schema layer; agent_definitions is the one table here that
    # already uses DB CHECKs, kept below).
    op.add_column("organization_settings", sa.Column("default_temperature", sa.Float(), nullable=True))
    op.add_column("conversations", sa.Column("temperature", sa.Float(), nullable=True))
    # NULL now means "inherit the organization default" instead of being
    # disallowed — the existing ck_agent_definitions_temperature CHECK already
    # passes on NULL (Postgres CHECK semantics), so it needs no change.
    op.alter_column("agent_definitions", "temperature", nullable=True)


def downgrade() -> None:
    op.alter_column("agent_definitions", "temperature", nullable=False)
    op.drop_column("conversations", "temperature")
    op.drop_column("organization_settings", "default_temperature")
