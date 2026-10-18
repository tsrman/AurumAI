"""add human-in-the-loop confirmation support

Revision ID: e7b2c4f9a1d3
Revises: d3f7a1c9e4b6
Create Date: 2026-09-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7b2c4f9a1d3'
down_revision: Union[str, None] = 'd3f7a1c9e4b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'workflow_nodes',
        sa.Column(
            'requires_confirmation',
            sa.Boolean(),
            nullable=False,
            server_default='false',
        ),
    )

    op.drop_constraint('ck_workflow_node_runs_status', 'workflow_node_runs', type_='check')
    op.create_check_constraint(
        'ck_workflow_node_runs_status',
        'workflow_node_runs',
        "status IN ('pending', 'running', 'waiting_human', 'completed', "
        "'failed', 'cancelled')",
    )


def downgrade() -> None:
    op.drop_constraint('ck_workflow_node_runs_status', 'workflow_node_runs', type_='check')
    op.create_check_constraint(
        'ck_workflow_node_runs_status',
        'workflow_node_runs',
        "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
    )

    op.drop_column('workflow_nodes', 'requires_confirmation')
