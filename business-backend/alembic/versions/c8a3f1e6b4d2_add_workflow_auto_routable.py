"""add workflow_definitions.auto_routable

Revision ID: c8a3f1e6b4d2
Revises: a1c9e4b6d3f7
Create Date: 2026-09-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8a3f1e6b4d2'
down_revision: Union[str, None] = 'a1c9e4b6d3f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'workflow_definitions',
        sa.Column(
            'auto_routable',
            sa.Boolean(),
            nullable=False,
            server_default='false',
        ),
    )


def downgrade() -> None:
    op.drop_column('workflow_definitions', 'auto_routable')
