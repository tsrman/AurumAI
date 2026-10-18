"""add file_metadata

Revision ID: 2026_08_29_file_metadata
Revises: 2026_08_29_audit_logs
Create Date: 2026-08-29 15:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '2026_08_29_file_metadata'
down_revision: Union[str, None] = '2026_08_29_audit_logs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Metadata for files uploaded into object storage. The source file itself
# is in MinIO; this table stores name, size, type, uploader and the
# corresponding rag-api document. It is organization-scoped and follows
# the other tenant tables: app.current_org_id policy plus FORCE RLS.


def upgrade() -> None:
    op.create_table(
        "file_metadata",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=127), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_file_metadata_org_id", "file_metadata", ["org_id"])
    op.create_index("ix_file_metadata_document_id", "file_metadata", ["document_id"])
    op.create_unique_constraint("uq_file_metadata_storage_key", "file_metadata", ["storage_key"])

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON file_metadata TO aurum_ai_app")
    op.execute("ALTER TABLE file_metadata ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY file_metadata_org_isolation ON file_metadata
        USING (org_id::text = current_setting('app.current_org_id', true))
        WITH CHECK (org_id::text = current_setting('app.current_org_id', true))
        """
    )
    op.execute("ALTER TABLE file_metadata FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS file_metadata_org_isolation ON file_metadata")
    op.drop_constraint("uq_file_metadata_storage_key", "file_metadata", type_="unique")
    op.drop_index("ix_file_metadata_document_id", table_name="file_metadata")
    op.drop_index("ix_file_metadata_org_id", table_name="file_metadata")
    op.drop_table("file_metadata")
