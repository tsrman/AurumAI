"""add owning_conversation_id to knowledge_bases and skills

Revision ID: d3f7a1c9e4b6
Revises: 2026_09_03_workflow_diag_sweep
Create Date: 2026-09-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd3f7a1c9e4b6'
down_revision: Union[str, None] = '2026_09_03_workflow_diag_sweep'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'knowledge_bases',
        sa.Column('owning_conversation_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        'fk_knowledge_bases_owning_conversation_id',
        'knowledge_bases',
        'conversations',
        ['owning_conversation_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.create_unique_constraint(
        'uq_knowledge_bases_owning_conversation_id',
        'knowledge_bases',
        ['owning_conversation_id'],
    )

    op.add_column(
        'skills',
        sa.Column('owning_conversation_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        'fk_skills_owning_conversation_id',
        'skills',
        'conversations',
        ['owning_conversation_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.create_unique_constraint(
        'uq_skills_owning_conversation_id',
        'skills',
        ['owning_conversation_id'],
    )


def downgrade() -> None:
    op.drop_constraint('uq_skills_owning_conversation_id', 'skills', type_='unique')
    op.drop_constraint('fk_skills_owning_conversation_id', 'skills', type_='foreignkey')
    op.drop_column('skills', 'owning_conversation_id')

    op.drop_constraint(
        'uq_knowledge_bases_owning_conversation_id', 'knowledge_bases', type_='unique'
    )
    op.drop_constraint(
        'fk_knowledge_bases_owning_conversation_id', 'knowledge_bases', type_='foreignkey'
    )
    op.drop_column('knowledge_bases', 'owning_conversation_id')
