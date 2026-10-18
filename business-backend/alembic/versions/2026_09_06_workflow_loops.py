"""add loop-back edge support (is_loop_back, max_iterations)

Revision ID: b3d9f2a5c8e1
Revises: f3a6c9e2b5d8
Create Date: 2026-09-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3d9f2a5c8e1'
down_revision: Union[str, None] = 'f3a6c9e2b5d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'workflow_edges',
        sa.Column('is_loop_back', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.add_column(
        'workflow_edges',
        sa.Column('max_iterations', sa.SmallInteger(), nullable=True),
    )

    op.drop_constraint('ck_workflow_edges_no_self_edge', 'workflow_edges', type_='check')
    op.create_check_constraint(
        'ck_workflow_edges_no_self_edge',
        'workflow_edges',
        'is_loop_back OR source_node_id <> target_node_id',
    )
    op.create_check_constraint(
        'ck_workflow_edges_max_iterations',
        'workflow_edges',
        '(is_loop_back AND max_iterations BETWEEN 2 AND 20) '
        'OR (NOT is_loop_back AND max_iterations IS NULL)',
    )


def downgrade() -> None:
    op.drop_constraint('ck_workflow_edges_max_iterations', 'workflow_edges', type_='check')
    op.drop_constraint('ck_workflow_edges_no_self_edge', 'workflow_edges', type_='check')
    op.create_check_constraint(
        'ck_workflow_edges_no_self_edge',
        'workflow_edges',
        'source_node_id <> target_node_id',
    )

    op.drop_column('workflow_edges', 'max_iterations')
    op.drop_column('workflow_edges', 'is_loop_back')
