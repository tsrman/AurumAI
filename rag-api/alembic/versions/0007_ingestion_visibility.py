"""Record what a finished document lacks: context and truncated chunks.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE documents "
        "ADD COLUMN uncontextualized_chunks integer NOT NULL DEFAULT 0, "
        "ADD COLUMN truncated_chunks integer NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE documents "
        "ADD CONSTRAINT ck_documents_visibility_counts "
        "CHECK (uncontextualized_chunks >= 0 AND truncated_chunks >= 0)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP CONSTRAINT ck_documents_visibility_counts")
    op.execute(
        "ALTER TABLE documents DROP COLUMN truncated_chunks, DROP COLUMN uncontextualized_chunks"
    )
