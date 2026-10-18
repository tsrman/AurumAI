"""add skill mcp auth token

Revision ID: 70ed9063ed6d
Revises: fd179f7a0e2e
Create Date: 2026-07-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '70ed9063ed6d'
down_revision: Union[str, None] = 'fd179f7a0e2e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('skills', sa.Column('mcp_auth_token', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('skills', 'mcp_auth_token')
