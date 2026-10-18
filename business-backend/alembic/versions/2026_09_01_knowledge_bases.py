"""add canonical knowledge bases

Revision ID: 2026_09_01_knowledge_bases
Revises: 2026_09_01_auth_v2_backfill
Create Date: 2026-09-01 03:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2026_09_01_knowledge_bases"
down_revision: Union[str, None] = "2026_09_01_auth_v2_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(name)) > 0",
            name="ck_knowledge_bases_name_nonblank",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "name",
            name="uq_knowledge_bases_org_name",
        ),
    )
    op.create_index(
        "ix_knowledge_bases_org_id_created_at",
        "knowledge_bases",
        ["org_id", "created_at"],
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge_bases TO aurum_ai_app"
    )
    op.execute("ALTER TABLE knowledge_bases ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY knowledge_bases_org_isolation ON knowledge_bases
        USING (org_id::text = current_setting('app.current_org_id', true))
        WITH CHECK (org_id::text = current_setting('app.current_org_id', true))
        """
    )
    op.execute("ALTER TABLE knowledge_bases FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS knowledge_bases_org_isolation ON knowledge_bases"
    )
    op.drop_index(
        "ix_knowledge_bases_org_id_created_at",
        table_name="knowledge_bases",
    )
    op.drop_table("knowledge_bases")
