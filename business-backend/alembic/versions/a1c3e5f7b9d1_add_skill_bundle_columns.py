"""add skill bundle columns

Revision ID: a1c3e5f7b9d1
Revises: 70ed9063ed6d
Create Date: 2026-07-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c3e5f7b9d1'
down_revision: Union[str, None] = '70ed9063ed6d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('skills', sa.Column('bundle_storage_key', sa.String(500), nullable=True))
    op.add_column('skills', sa.Column('bundle_checksum', sa.String(64), nullable=True))
    op.add_column('skills', sa.Column('bundle_size_bytes', sa.Integer(), nullable=True))
    op.add_column('skills', sa.Column('bundle_skill_name', sa.String(255), nullable=True))
    op.add_column(
        'skills',
        sa.Column('network_policy', sa.String(20), nullable=False, server_default='none'),
    )


def downgrade() -> None:
    op.drop_column('skills', 'network_policy')
    op.drop_column('skills', 'bundle_skill_name')
    op.drop_column('skills', 'bundle_size_bytes')
    op.drop_column('skills', 'bundle_checksum')
    op.drop_column('skills', 'bundle_storage_key')
