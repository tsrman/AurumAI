"""drop anonymizer_enforced column

Revision ID: 958b310beac2
Revises: f6a0207325ff
Create Date: 2026-06-21 07:44:41.388587

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '958b310beac2'
down_revision: Union[str, None] = 'f6a0207325ff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column('organizations', 'anonymizer_enforced')


def downgrade() -> None:
    op.add_column(
        'organizations',
        sa.Column('anonymizer_enforced', sa.Boolean(), nullable=True),
    )
