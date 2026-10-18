"""add documents table

Revision ID: 41134eb36343
Revises: 2026_06_22_add_document_chunks
Create Date: 2026-07-05 09:28:40.059941

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '41134eb36343'
down_revision: Union[str, None] = '2026_06_22_add_document_chunks'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_documents_org_id"), "documents", ["org_id"], unique=False)

    op.add_column("document_chunks", sa.Column("document_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_document_chunks_document_id_documents"),
        "document_chunks",
        "documents",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.execute("UPDATE document_chunks SET document_id = '00000000-0000-0000-0000-000000000000'")
    op.alter_column("document_chunks", "document_id", nullable=False)

    # RLS
    op.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS documents_org_isolation ON documents")
    op.execute(
        """
        CREATE POLICY documents_org_isolation ON documents
        USING (org_id::text = current_setting('app.current_org_id', true))
        WITH CHECK (org_id::text = current_setting('app.current_org_id', true))
        """
    )
    op.execute("ALTER TABLE documents FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_constraint(op.f("fk_document_chunks_document_id_documents"), "document_chunks", type_="foreignkey")
    op.drop_column("document_chunks", "document_id")
    op.drop_index(op.f("ix_documents_org_id"), table_name="documents")
    op.drop_table("documents")
