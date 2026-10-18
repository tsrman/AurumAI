"""drop documents tables

Revision ID: 2026_07_18_drop_documents_tables
Revises: 41134eb36343
Create Date: 2026-07-18 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '2026_07_18_drop_documents_tables'
down_revision: Union[str, None] = 'b2d4f6a8c0e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table('document_chunks')
    op.drop_table('documents')


def downgrade() -> None:
    # Not implementing downgrade for table drops, data is lost
    pass
