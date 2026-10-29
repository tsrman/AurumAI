"""Agent definitions of kind hermes (Hermes Agent workflow nodes).

Revision ID: 20261031_agent_definition_kind
Revises: 20261030_hermes_skill_drafts
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261031_agent_definition_kind"
down_revision: str | None = "20261030_hermes_skill_drafts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_definitions ADD COLUMN kind text NOT NULL DEFAULT 'agent'")
    op.execute(
        "ALTER TABLE agent_definitions ADD CONSTRAINT ck_agent_definitions_kind "
        "CHECK (kind IN ('agent', 'hermes'))"
    )
    # Hermes Agent uses the platform's tools and its own skills, never an
    # agent definition's.
    op.execute(
        "ALTER TABLE agent_definitions ADD CONSTRAINT ck_agent_definitions_hermes_tools CHECK ("
        "kind = 'agent' OR (cardinality(skill_ids) = 0 AND cardinality(knowledge_base_ids) = 0))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_definitions DROP CONSTRAINT ck_agent_definitions_hermes_tools")
    op.execute("ALTER TABLE agent_definitions DROP CONSTRAINT ck_agent_definitions_kind")
    op.execute("ALTER TABLE agent_definitions DROP COLUMN kind")
