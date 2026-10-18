"""add source_key to documents

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-29 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

# Documents originate from inline content or an object-storage upload.
# Uploaded-file records have no inline content; their key lets rag-worker
# download and parse the source through Docling
# (rag-worker/src/activities.py). Both fields are nullable individually,
# while document creation requires exactly one to be populated.


def upgrade() -> None:
    op.add_column('documents', sa.Column('source_key', sa.String(length=1024), nullable=True))
    op.add_column('documents', sa.Column('source_filename', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('documents', 'source_filename')
    op.drop_column('documents', 'source_key')
