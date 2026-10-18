"""add skill description

Revision ID: fd179f7a0e2e
Revises: 41134eb36343
Create Date: 2026-07-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fd179f7a0e2e'
down_revision: Union[str, None] = '41134eb36343'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('skills', sa.Column('description', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('skills', 'description')
