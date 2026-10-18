"""add document chunks

Revision ID: 2026_06_22_add_document_chunks
Revises: 2026_06_21_uq_msg_turn_role
Create Date: 2026-06-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "2026_06_22_add_document_chunks"
down_revision: Union[str, None] = "2026_06_21_uq_msg_turn_role"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("metadata_json", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_document_chunks_org_id"), "document_chunks", ["org_id"], unique=False)

    # RLS Policies
    op.execute("ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS document_chunks_org_isolation ON document_chunks")
    op.execute(
        """
        CREATE POLICY document_chunks_org_isolation ON document_chunks
        USING (org_id::text = current_setting('app.current_org_id', true))
        WITH CHECK (org_id::text = current_setting('app.current_org_id', true))
        """
    )
    op.execute("ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index(op.f("ix_document_chunks_org_id"), table_name="document_chunks")
    op.drop_table("document_chunks")
