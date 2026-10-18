"""add workflow templates and workflow provenance

Revision ID: f3a6c9e2b5d8
Revises: c8a3f1e6b4d2
Create Date: 2026-09-05 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "f3a6c9e2b5d8"
down_revision: str | None = "c8a3f1e6b4d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
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
            name="ck_workflow_templates_name_nonblank",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_workflow_templates_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_workflow_templates_version"),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_workflow_templates_snapshot_object",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "name",
            name="uq_workflow_templates_org_name",
        ),
        sa.UniqueConstraint(
            "org_id",
            "id",
            name="uq_workflow_templates_org_id_id",
        ),
    )
    op.create_index(
        "ix_workflow_templates_org_status_name",
        "workflow_templates",
        ["org_id", "status", "name"],
    )

    op.execute("ALTER TABLE workflow_templates ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workflow_templates FORCE ROW LEVEL SECURITY")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON workflow_templates TO aurum_ai_app"
    )
    op.execute(
        """
        CREATE POLICY workflow_templates_org_isolation ON workflow_templates
        USING (org_id::text = current_setting('app.current_org_id', true))
        WITH CHECK (org_id::text = current_setting('app.current_org_id', true))
        """
    )

    op.add_column(
        "workflow_definitions",
        sa.Column("source_template_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "workflow_definitions",
        sa.Column("source_template_version", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_workflow_definitions_org_source_template",
        "workflow_definitions",
        "workflow_templates",
        ["org_id", "source_template_id"],
        ["org_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_workflow_definitions_source_template_provenance",
        "workflow_definitions",
        "(source_template_id IS NULL AND source_template_version IS NULL) "
        "OR (source_template_id IS NOT NULL "
        "AND source_template_version IS NOT NULL "
        "AND source_template_version >= 1)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_workflow_definitions_org_source_template",
        "workflow_definitions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_workflow_definitions_source_template_provenance",
        "workflow_definitions",
        type_="check",
    )
    op.drop_column("workflow_definitions", "source_template_version")
    op.drop_column("workflow_definitions", "source_template_id")

    op.execute(
        "DROP POLICY IF EXISTS workflow_templates_org_isolation ON workflow_templates"
    )
    op.drop_index(
        "ix_workflow_templates_org_status_name",
        table_name="workflow_templates",
    )
    op.drop_table("workflow_templates")
