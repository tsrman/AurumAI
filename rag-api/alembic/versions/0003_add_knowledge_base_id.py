"""stage knowledge_base_id on documents

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-01 03:15:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Deliberately nullable for exactly one rollout stage. Operators must map
    # every legacy row to an explicitly created aurum_ai knowledge-base
    # UUID before applying 0004; no cross-database default can be inferred.
    op.add_column(
        "documents",
        sa.Column(
            "knowledge_base_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_documents_knowledge_base_id",
        "documents",
        ["knowledge_base_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_documents_knowledge_base_id", table_name="documents")
    op.drop_column("documents", "knowledge_base_id")
