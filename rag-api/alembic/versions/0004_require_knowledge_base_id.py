"""require explicit knowledge-base mapping for every document

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-01 03:30:00.000000

"""

from typing import Protocol

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


class _ScalarResult(Protocol):
    def scalar_one(self) -> int: ...


class _Connection(Protocol):
    def execute(self, statement: object) -> _ScalarResult: ...


def require_explicit_knowledge_base_backfill(connection: _Connection) -> None:
    unmapped = connection.execute(
        sa.text("SELECT count(*) FROM documents WHERE knowledge_base_id IS NULL")
    ).scalar_one()
    if unmapped:
        raise RuntimeError(
            f"{unmapped} document(s) have no knowledge_base_id. Stop at rag-api "
            "revision 0003, create or select the canonical knowledge base in "
            "aurum_ai, explicitly UPDATE every legacy rag_platform.documents "
            "row, then apply revision 0004. No default is inferred."
        )


def upgrade() -> None:
    require_explicit_knowledge_base_backfill(op.get_bind())
    op.alter_column(
        "documents",
        "knowledge_base_id",
        existing_type=sa.UUID(),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "documents",
        "knowledge_base_id",
        existing_type=sa.UUID(),
        nullable=True,
    )
