"""Image tools: capability, operation kinds, conversation images, org models.

Revision ID: 20261024_image_tools
Revises: 20260913_archives
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20261024_image_tools"
down_revision: str | None = "20260913_archives"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_CAPABILITIES = (
    "'admin.users.read', 'admin.users.write', 'chat.send', 'sharing.manage', 'web.access', "
    "'workspace.agents.create', 'workspace.knowledge.create', "
    "'workspace.skills.create'"
)
NEW_CAPABILITIES = (
    "'admin.users.read', 'admin.users.write', 'chat.send', 'images.generate', "
    "'sharing.manage', 'web.access', 'workspace.agents.create', "
    "'workspace.knowledge.create', 'workspace.skills.create'"
)
CAPABILITY_CONSTRAINTS = (
    ("organization_capabilities", "ck_organization_capabilities_capability"),
    ("group_capabilities", "ck_group_capabilities_capability"),
)
OLD_KINDS = "'ask_user','web_search','web_read'"
NEW_KINDS = "'ask_user','web_search','web_read','image_generate','image_edit'"

TABLE = "conversation_images"
DDL = """
CREATE TABLE conversation_images (
 id uuid PRIMARY KEY, org_id uuid NOT NULL, conversation_id uuid NOT NULL,
 turn_id uuid, execution_id uuid, operation_id uuid,
 origin text NOT NULL, source_image_id uuid,
 storage_key text NOT NULL, content_type text NOT NULL, size_bytes bigint NOT NULL,
 width integer, height integer, prompt text, model text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT uq_conversation_image_org_id UNIQUE (org_id, id),
 CONSTRAINT uq_conversation_image_storage_key UNIQUE (storage_key),
 CONSTRAINT uq_conversation_image_operation UNIQUE (org_id, operation_id),
 CONSTRAINT fk_conversation_image_conversation FOREIGN KEY (org_id, conversation_id)
   REFERENCES conversation_identities(org_id, id) ON DELETE CASCADE,
 CONSTRAINT fk_conversation_image_operation FOREIGN KEY (org_id, turn_id, execution_id, operation_id)
   REFERENCES tool_operations(org_id, turn_id, execution_id, id) ON DELETE CASCADE,
 CONSTRAINT fk_conversation_image_source FOREIGN KEY (org_id, source_image_id)
   REFERENCES conversation_images(org_id, id) ON DELETE SET NULL (source_image_id),
 CONSTRAINT ck_conversation_image_origin CHECK (origin IN ('generated','uploaded')),
 CONSTRAINT ck_conversation_image_provenance CHECK (
   (origin = 'generated' AND turn_id IS NOT NULL AND execution_id IS NOT NULL
     AND operation_id IS NOT NULL)
   OR (origin = 'uploaded' AND operation_id IS NULL)),
 CONSTRAINT ck_conversation_image_content_type CHECK (
   content_type IN ('image/png','image/jpeg','image/webp')),
 CONSTRAINT ck_conversation_image_size CHECK (size_bytes > 0),
 CONSTRAINT ck_conversation_image_dimensions CHECK (
   (width IS NULL OR width > 0) AND (height IS NULL OR height > 0)),
 CONSTRAINT ck_conversation_image_storage_key CHECK (length(storage_key) BETWEEN 1 AND 1024),
 CONSTRAINT ck_conversation_image_prompt CHECK (prompt IS NULL OR length(prompt) <= 4000),
 CONSTRAINT ck_conversation_image_model CHECK (model IS NULL OR length(model) <= 255)
);
CREATE INDEX ix_conversation_images_org_id ON conversation_images(org_id);
CREATE INDEX ix_conversation_images_conversation
 ON conversation_images(org_id, conversation_id, created_at, id)
"""


def _replace(table: str, name: str, condition: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, condition)


def upgrade() -> None:
    for table, name in CAPABILITY_CONSTRAINTS:
        _replace(table, name, f"capability IN ({NEW_CAPABILITIES})")
    _replace("tool_operations", "ck_tool_operation_kind", f"kind IN ({NEW_KINDS})")
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO aurum_ai_app")
    # The lifecycle worker reads every conversation-scoped table; its blanket
    # grant only covered tables that existed when it was created.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO aurum_ai_lifecycle")
    op.execute(
        f"CREATE POLICY {TABLE}_org_isolation ON {TABLE} "
        "USING (org_id::text = current_setting('app.current_org_id', true)) "
        "WITH CHECK (org_id::text = current_setting('app.current_org_id', true))"
    )
    op.add_column(
        "interaction_turns",
        sa.Column("image_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        "ck_interaction_turn_image_count", "interaction_turns", "image_count >= 0"
    )
    op.add_column(
        "organization_settings", sa.Column("image_generation_model", sa.String(255))
    )
    op.add_column("organization_settings", sa.Column("image_edit_model", sa.String(255)))


def downgrade() -> None:
    op.drop_column("organization_settings", "image_edit_model")
    op.drop_column("organization_settings", "image_generation_model")
    op.drop_table(TABLE)
    op.drop_constraint("ck_interaction_turn_image_count", "interaction_turns", type_="check")
    op.drop_column("interaction_turns", "image_count")
    # Fails instead of deleting data while image operations or grants exist.
    _replace("tool_operations", "ck_tool_operation_kind", f"kind IN ({OLD_KINDS})")
    for table, name in CAPABILITY_CONSTRAINTS:
        _replace(table, name, f"capability IN ({OLD_CAPABILITIES})")
