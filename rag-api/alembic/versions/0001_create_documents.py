"""create documents table

Revision ID: 0001
Revises:
Create Date: 2026-07-18 19:15:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0001'
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    # Lives in its own `rag_platform` Postgres database (see
    # postgres-init-scripts/02-create-rag-db.sql), connected to as the
    # rag_app role — not a schema bolted onto the shared aurum_ai
    # database, and not the postgres superuser. Default `public` schema
    # inside that database is enough; no CREATE SCHEMA needed.
    op.create_table('documents',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('org_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('parsed_markdown', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('chunk_count', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_documents_org_id'), 'documents', ['org_id'], unique=False)

def downgrade() -> None:
    op.drop_index(op.f('ix_documents_org_id'), table_name='documents')
    op.drop_table('documents')
