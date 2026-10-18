"""Add durable web source provenance and expiring normalized content.

Revision ID: e8a1c4f7b2d5
Revises: d7f9b2c5a1e4
"""

from collections.abc import Sequence

from alembic import op


revision: str = "e8a1c4f7b2d5"
down_revision: str | None = "d7f9b2c5a1e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "web_sources"

DDL = """
CREATE TABLE web_sources (
 id uuid PRIMARY KEY, org_id uuid NOT NULL, turn_id uuid NOT NULL,
 execution_id uuid NOT NULL, operation_id uuid NOT NULL,
 position integer NOT NULL, kind text NOT NULL,
 requested_url text, final_url text NOT NULL, title text, content_type text,
 content text, published_at timestamptz, retrieved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 truncated boolean NOT NULL DEFAULT false, page_count integer,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT uq_web_source_turn_id UNIQUE (org_id,turn_id,id),
 CONSTRAINT uq_web_source_position UNIQUE (org_id,operation_id,position),
 CONSTRAINT fk_web_source_operation FOREIGN KEY (org_id,turn_id,execution_id,operation_id)
 REFERENCES tool_operations(org_id,turn_id,execution_id,id) ON DELETE CASCADE,
 CONSTRAINT ck_web_source_kind CHECK (kind IN ('search','read')),
 CONSTRAINT ck_web_source_position CHECK (position BETWEEN 0 AND 4),
 CONSTRAINT ck_web_source_final_url CHECK (length(final_url) BETWEEN 1 AND 8192),
 CONSTRAINT ck_web_source_requested_url CHECK (
   requested_url IS NULL OR length(requested_url) BETWEEN 1 AND 8192),
 CONSTRAINT ck_web_source_title CHECK (title IS NULL OR length(title) <= 2000),
 CONSTRAINT ck_web_source_content CHECK (content IS NULL OR length(content) <= 50000),
 CONSTRAINT ck_web_source_page_count CHECK (page_count IS NULL OR page_count >= 0)
);
CREATE INDEX ix_web_sources_org_id ON web_sources(org_id);
CREATE INDEX ix_web_sources_turn ON web_sources(org_id,turn_id,position,id);
CREATE INDEX ix_tool_operations_result_expiry
 ON tool_operations(result_expires_at,id) WHERE result IS NOT NULL;
"""


def upgrade() -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO aurum_ai_app")
    op.execute(
        f"CREATE POLICY {TABLE}_org_isolation ON {TABLE} "
        "USING (org_id::text = current_setting('app.current_org_id', true)) "
        "WITH CHECK (org_id::text = current_setting('app.current_org_id', true))"
    )


def downgrade() -> None:
    op.drop_index("ix_tool_operations_result_expiry", table_name="tool_operations")
    op.drop_table(TABLE)
