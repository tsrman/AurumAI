"""add conditional branching support (success/failure edges)

Revision ID: a1c9e4b6d3f7
Revises: e7b2c4f9a1d3
Create Date: 2026-09-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c9e4b6d3f7'
down_revision: Union[str, None] = 'e7b2c4f9a1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'workflow_edges',
        sa.Column(
            'condition',
            sa.String(length=16),
            nullable=False,
            server_default='on_success',
        ),
    )
    op.create_check_constraint(
        'ck_workflow_edges_condition',
        'workflow_edges',
        "condition IN ('on_success', 'on_failure')",
    )

    op.drop_constraint('ck_workflow_node_runs_status', 'workflow_node_runs', type_='check')
    op.create_check_constraint(
        'ck_workflow_node_runs_status',
        'workflow_node_runs',
        "status IN ('pending', 'running', 'waiting_human', 'completed', "
        "'failed', 'skipped', 'cancelled')",
    )


def downgrade() -> None:
    op.drop_constraint('ck_workflow_node_runs_status', 'workflow_node_runs', type_='check')
    op.create_check_constraint(
        'ck_workflow_node_runs_status',
        'workflow_node_runs',
        "status IN ('pending', 'running', 'waiting_human', 'completed', "
        "'failed', 'cancelled')",
    )

    op.drop_constraint('ck_workflow_edges_condition', 'workflow_edges', type_='check')
    op.drop_column('workflow_edges', 'condition')
