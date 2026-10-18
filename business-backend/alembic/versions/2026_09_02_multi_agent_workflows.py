"""add persistent multi-agent workflow definitions and runs

Revision ID: 2026_09_02_multi_agent_workflows
Revises: 2026_09_02_org_analytics
Create Date: 2026-09-02 15:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "2026_09_02_multi_agent_workflows"
down_revision: str | None = "2026_09_02_org_analytics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


WORKFLOW_TABLES = (
    "workflow_definitions",
    "workflow_nodes",
    "workflow_edges",
    "workflow_runs",
    "workflow_node_runs",
)


def _enable_org_rls(table: str) -> None:
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO aurum_ai_app")
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY {table}_org_isolation ON {table}
        USING (org_id::text = current_setting('app.current_org_id', true))
        WITH CHECK (org_id::text = current_setting('app.current_org_id', true))
        """
    )
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    op.alter_column(
        "messages",
        "model",
        existing_type=sa.String(length=100),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.create_unique_constraint(
        "uq_agent_definitions_org_id_id", "agent_definitions", ["org_id", "id"]
    )
    op.create_unique_constraint("uq_users_org_id_id", "users", ["org_id", "id"])
    op.create_unique_constraint(
        "uq_conversations_org_id_id", "conversations", ["org_id", "id"]
    )
    op.create_unique_constraint("uq_messages_org_id_id", "messages", ["org_id", "id"])

    op.create_table(
        "workflow_definitions",
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
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "supervisor_agent_definition_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("max_parallelism", sa.SmallInteger(), server_default="4", nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'active'"),
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
            "length(btrim(name)) > 0", name="ck_workflow_definitions_name_nonblank"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived')", name="ck_workflow_definitions_status"
        ),
        sa.CheckConstraint("version >= 1", name="ck_workflow_definitions_version"),
        sa.CheckConstraint(
            "max_parallelism >= 1 AND max_parallelism <= 4",
            name="ck_workflow_definitions_parallelism",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "supervisor_agent_definition_id"],
            ["agent_definitions.org_id", "agent_definitions.id"],
            name="fk_workflow_definitions_org_supervisor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "name", name="uq_workflow_definitions_org_name"),
        sa.UniqueConstraint("org_id", "id", name="uq_workflow_definitions_org_id_id"),
    )
    op.create_index(
        "ix_workflow_definitions_org_status_name",
        "workflow_definitions",
        ["org_id", "status", "name"],
    )

    op.create_table(
        "workflow_nodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workflow_definition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("agent_definition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("position_x", sa.Float(), nullable=True),
        sa.Column("position_y", sa.Float(), nullable=True),
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
            "node_key ~ '^[a-z][a-z0-9_-]{0,63}$'", name="ck_workflow_nodes_key"
        ),
        sa.CheckConstraint(
            "length(btrim(display_name)) > 0",
            name="ck_workflow_nodes_display_name_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(instruction)) > 0",
            name="ck_workflow_nodes_instruction_nonblank",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workflow_definition_id"],
            ["workflow_definitions.org_id", "workflow_definitions.id"],
            name="fk_workflow_nodes_org_definition",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_definition_id"],
            ["agent_definitions.org_id", "agent_definitions.id"],
            name="fk_workflow_nodes_org_agent",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_definition_id",
            "node_key",
            name="uq_workflow_nodes_definition_key",
        ),
        sa.UniqueConstraint(
            "org_id",
            "workflow_definition_id",
            "id",
            name="uq_workflow_nodes_org_definition_id",
        ),
    )
    op.create_index(
        "ix_workflow_nodes_org_definition",
        "workflow_nodes",
        ["org_id", "workflow_definition_id"],
    )

    op.create_table(
        "workflow_edges",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workflow_definition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_node_id", postgresql.UUID(as_uuid=True), nullable=False),
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
            "source_node_id <> target_node_id", name="ck_workflow_edges_no_self_edge"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workflow_definition_id"],
            ["workflow_definitions.org_id", "workflow_definitions.id"],
            name="fk_workflow_edges_org_definition",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workflow_definition_id", "source_node_id"],
            [
                "workflow_nodes.org_id",
                "workflow_nodes.workflow_definition_id",
                "workflow_nodes.id",
            ],
            name="fk_workflow_edges_source_node",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workflow_definition_id", "target_node_id"],
            [
                "workflow_nodes.org_id",
                "workflow_nodes.workflow_definition_id",
                "workflow_nodes.id",
            ],
            name="fk_workflow_edges_target_node",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_definition_id",
            "source_node_id",
            "target_node_id",
            name="uq_workflow_edges_definition_pair",
        ),
    )
    op.create_index(
        "ix_workflow_edges_org_definition",
        "workflow_edges",
        ["org_id", "workflow_definition_id"],
    )

    op.create_table(
        "workflow_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workflow_definition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_definition_version", sa.Integer(), nullable=False),
        sa.Column("initiating_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("temporal_workflow_id", sa.String(length=255), nullable=False),
        sa.Column("execution_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("internal_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            "workflow_definition_version >= 1",
            name="ck_workflow_runs_definition_version",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'planning', 'running', 'synthesizing', "
            "'completed', 'failed', 'cancelled')",
            name="ck_workflow_runs_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(execution_snapshot) = 'object'",
            name="ck_workflow_runs_snapshot_object",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workflow_definition_id"],
            ["workflow_definitions.org_id", "workflow_definitions.id"],
            name="fk_workflow_runs_org_definition",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "initiating_user_id"],
            ["users.org_id", "users.id"],
            name="fk_workflow_runs_org_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "conversation_id"],
            ["conversations.org_id", "conversations.id"],
            name="fk_workflow_runs_org_conversation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "source_message_id"],
            ["messages.org_id", "messages.id"],
            name="fk_workflow_runs_org_message",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_workflow_runs_org_id_id"),
        sa.UniqueConstraint(
            "conversation_id", "turn_id", name="uq_workflow_runs_conversation_turn"
        ),
        sa.UniqueConstraint(
            "temporal_workflow_id", name="uq_workflow_runs_temporal_workflow_id"
        ),
    )
    op.create_index(
        "ix_workflow_runs_org_user_created",
        "workflow_runs",
        ["org_id", "initiating_user_id", "created_at"],
    )
    op.create_index(
        "ix_workflow_runs_org_status_created",
        "workflow_runs",
        ["org_id", "status", "created_at"],
    )

    op.create_table(
        "workflow_node_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workflow_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phase_key", sa.String(length=64), nullable=False),
        sa.Column("agent_definition_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_name", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("internal_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("diagnostic_expires_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_workflow_node_runs_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_workflow_node_runs_attempt_count"
        ),
        sa.CheckConstraint(
            "length(btrim(phase_key)) > 0",
            name="ck_workflow_node_runs_phase_key_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(agent_name)) > 0",
            name="ck_workflow_node_runs_agent_name_nonblank",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "workflow_run_id"],
            ["workflow_runs.org_id", "workflow_runs.id"],
            name="fk_workflow_node_runs_org_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "agent_definition_id"],
            ["agent_definitions.org_id", "agent_definitions.id"],
            name="fk_workflow_node_runs_org_agent",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_run_id", "phase_key", name="uq_workflow_node_runs_run_phase"
        ),
    )
    op.create_index(
        "ix_workflow_node_runs_org_run",
        "workflow_node_runs",
        ["org_id", "workflow_run_id"],
    )
    op.create_index(
        "ix_workflow_node_runs_diagnostic_expiry",
        "workflow_node_runs",
        ["diagnostic_expires_at"],
    )

    op.add_column(
        "llm_usage_events",
        sa.Column("workflow_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "llm_usage_events", sa.Column("workflow_phase", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "llm_usage_events",
        sa.Column("workflow_node_key", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_llm_usage_events_workflow_run",
        "llm_usage_events",
        "workflow_runs",
        ["org_id", "workflow_run_id"],
        ["org_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_llm_usage_events_workflow_correlation",
        "llm_usage_events",
        "(workflow_run_id IS NULL AND workflow_phase IS NULL "
        "AND workflow_node_key IS NULL) OR "
        "(workflow_run_id IS NOT NULL AND "
        "((workflow_phase = 'node' AND workflow_node_key IS NOT NULL) OR "
        "(workflow_phase IN ('supervisor_prepare', 'supervisor_synthesize') "
        "AND workflow_node_key IS NULL)))",
    )
    op.create_index(
        "ix_llm_usage_events_workflow_run_id",
        "llm_usage_events",
        ["workflow_run_id"],
    )

    op.drop_constraint(
        "ck_access_grants_resource_type", "access_grants", type_="check"
    )
    op.create_check_constraint(
        "ck_access_grants_resource_type",
        "access_grants",
        "resource_type IN "
        "('model', 'skill', 'knowledge_base', 'agent_definition', 'workflow')",
    )

    for table in WORKFLOW_TABLES:
        _enable_org_rls(table)


def downgrade() -> None:
    has_oversized_message_model = op.get_bind().execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM messages WHERE char_length(model) > 100)"
        )
    ).scalar_one()
    if has_oversized_message_model:
        raise RuntimeError(
            "Cannot downgrade workflow support: a persisted message model ID "
            "exceeds the legacy 100-character limit"
        )

    for table in reversed(WORKFLOW_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {table}_org_isolation ON {table}")

    op.drop_constraint(
        "ck_access_grants_resource_type", "access_grants", type_="check"
    )
    op.create_check_constraint(
        "ck_access_grants_resource_type",
        "access_grants",
        "resource_type IN ('model', 'skill', 'knowledge_base', 'agent_definition')",
    )

    op.drop_index("ix_llm_usage_events_workflow_run_id", table_name="llm_usage_events")
    op.drop_constraint(
        "ck_llm_usage_events_workflow_correlation",
        "llm_usage_events",
        type_="check",
    )
    op.drop_constraint(
        "fk_llm_usage_events_workflow_run", "llm_usage_events", type_="foreignkey"
    )
    op.drop_column("llm_usage_events", "workflow_node_key")
    op.drop_column("llm_usage_events", "workflow_phase")
    op.drop_column("llm_usage_events", "workflow_run_id")

    op.drop_index(
        "ix_workflow_node_runs_diagnostic_expiry", table_name="workflow_node_runs"
    )
    op.drop_index("ix_workflow_node_runs_org_run", table_name="workflow_node_runs")
    op.drop_table("workflow_node_runs")
    op.drop_index("ix_workflow_runs_org_status_created", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_org_user_created", table_name="workflow_runs")
    op.drop_table("workflow_runs")
    op.drop_index("ix_workflow_edges_org_definition", table_name="workflow_edges")
    op.drop_table("workflow_edges")
    op.drop_index("ix_workflow_nodes_org_definition", table_name="workflow_nodes")
    op.drop_table("workflow_nodes")
    op.drop_index(
        "ix_workflow_definitions_org_status_name", table_name="workflow_definitions"
    )
    op.drop_table("workflow_definitions")

    op.drop_constraint("uq_messages_org_id_id", "messages", type_="unique")
    op.drop_constraint("uq_conversations_org_id_id", "conversations", type_="unique")
    op.drop_constraint("uq_users_org_id_id", "users", type_="unique")
    op.drop_constraint(
        "uq_agent_definitions_org_id_id", "agent_definitions", type_="unique"
    )
    op.alter_column(
        "messages",
        "model",
        existing_type=sa.String(length=255),
        type_=sa.String(length=100),
        existing_nullable=True,
    )
