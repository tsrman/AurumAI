"""add persistent agent definitions

Revision ID: 2026_09_01_agent_definitions
Revises: 2026_09_01_knowledge_bases
Create Date: 2026-09-01 10:45:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2026_09_01_agent_definitions"
down_revision: Union[str, None] = "2026_09_01_knowledge_bases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
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
        sa.Column("model_id", sa.String(length=255), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column(
            "temperature",
            sa.Float(),
            server_default="0.3",
            nullable=False,
        ),
        sa.Column(
            "skill_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "knowledge_base_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            server_default="{}",
            nullable=False,
        ),
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
            name="ck_agent_definitions_name_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(model_id)) > 0",
            name="ck_agent_definitions_model_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(system_prompt)) > 0",
            name="ck_agent_definitions_prompt_nonblank",
        ),
        sa.CheckConstraint(
            "temperature >= 0 AND temperature <= 2",
            name="ck_agent_definitions_temperature",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "name",
            name="uq_agent_definitions_org_name",
        ),
    )
    op.create_index(
        "ix_agent_definitions_org_id_created_at",
        "agent_definitions",
        ["org_id", "created_at"],
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON agent_definitions TO aurum_ai_app"
    )
    op.execute("ALTER TABLE agent_definitions ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY agent_definitions_org_isolation ON agent_definitions
        USING (org_id::text = current_setting('app.current_org_id', true))
        WITH CHECK (org_id::text = current_setting('app.current_org_id', true))
        """
    )
    op.execute("ALTER TABLE agent_definitions FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS agent_definitions_org_isolation ON agent_definitions"
    )
    op.drop_index(
        "ix_agent_definitions_org_id_created_at",
        table_name="agent_definitions",
    )
    op.drop_table("agent_definitions")
