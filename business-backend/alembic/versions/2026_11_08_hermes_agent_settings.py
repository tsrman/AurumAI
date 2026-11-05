"""Settings of a Hermes-kind agent definition.

Revision ID: 20261108_hermes_agent_settings
Revises: 20261107_agent_capabilities

A Hermes agent may now carry knowledge bases and skills, choose its own model,
and lower the internet mode of the organization for its sessions. Existing
agents keep their behaviour: they use the organization's Hermes model and mode,
and every skill the user may run.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261108_hermes_agent_settings"
down_revision: str | None = "20261107_agent_capabilities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_definitions DROP CONSTRAINT ck_agent_definitions_hermes_tools")
    # NULL: follow the organization's mode. A value can only lower it.
    op.execute("ALTER TABLE agent_definitions ADD COLUMN hermes_egress_mode varchar(16)")
    op.execute(
        "ALTER TABLE agent_definitions ADD CONSTRAINT ck_agent_definitions_hermes_egress_mode "
        "CHECK (hermes_egress_mode IS NULL OR hermes_egress_mode IN ('none', 'packages', 'public'))"
    )
    op.execute("ALTER TABLE agent_definitions ADD COLUMN use_org_hermes_model boolean NOT NULL DEFAULT true")
    op.execute("ALTER TABLE agent_definitions ADD COLUMN hermes_skills_scope varchar(16) NOT NULL DEFAULT 'all'")
    op.execute(
        "ALTER TABLE agent_definitions ADD CONSTRAINT ck_agent_definitions_hermes_skills_scope "
        "CHECK (hermes_skills_scope IN ('all', 'selected'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_definitions DROP CONSTRAINT ck_agent_definitions_hermes_skills_scope")
    op.execute("ALTER TABLE agent_definitions DROP COLUMN hermes_skills_scope")
    op.execute("ALTER TABLE agent_definitions DROP COLUMN use_org_hermes_model")
    op.execute("ALTER TABLE agent_definitions DROP CONSTRAINT ck_agent_definitions_hermes_egress_mode")
    op.execute("ALTER TABLE agent_definitions DROP COLUMN hermes_egress_mode")
    op.execute(
        "UPDATE agent_definitions SET skill_ids = '{}', knowledge_base_ids = '{}' WHERE kind = 'hermes'"
    )
    op.execute(
        "ALTER TABLE agent_definitions ADD CONSTRAINT ck_agent_definitions_hermes_tools CHECK ("
        "kind = 'agent' OR (cardinality(skill_ids) = 0 AND cardinality(knowledge_base_ids) = 0))"
    )
