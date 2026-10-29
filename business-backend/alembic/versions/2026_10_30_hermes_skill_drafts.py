"""Hermes Agent skill drafts per user.

Revision ID: 20261030_hermes_skill_drafts
Revises: 20261029_hermes_memories
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261030_hermes_skill_drafts"
down_revision: str | None = "20261029_hermes_memories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "hermes_skill_drafts"
DDL = """
CREATE TABLE hermes_skill_drafts (
 id uuid PRIMARY KEY, org_id uuid NOT NULL, user_id uuid NOT NULL,
 path text NOT NULL, name text NOT NULL, description text NOT NULL,
 content_sha256 char(64) NOT NULL, file_count integer NOT NULL,
 bundle_storage_key text NOT NULL, bundle_checksum char(64) NOT NULL,
 bundle_size_bytes integer NOT NULL,
 status text NOT NULL DEFAULT 'pending',
 skill_id uuid REFERENCES skills(id) ON DELETE SET NULL,
 reviewed_by uuid, reviewed_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT fk_hermes_skill_draft_user FOREIGN KEY (org_id, user_id)
   REFERENCES users(org_id, id) ON DELETE CASCADE,
 CONSTRAINT ck_hermes_skill_draft_status CHECK (status IN ('pending','approved','rejected','dismissed')),
 CONSTRAINT ck_hermes_skill_draft_path CHECK (length(path) BETWEEN 1 AND 200),
 CONSTRAINT uq_hermes_skill_draft_content UNIQUE (org_id, user_id, content_sha256)
)
"""


def upgrade() -> None:
    op.execute(DDL)
    # One open draft per skill directory; a changed skill replaces it.
    op.execute(
        f"CREATE UNIQUE INDEX uq_hermes_skill_draft_pending ON {TABLE} (org_id, user_id, path) "
        "WHERE status = 'pending'"
    )
    op.execute(f"CREATE INDEX ix_hermes_skill_drafts_org_status ON {TABLE} (org_id, status, updated_at)")
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO aurum_ai_app")
    # The organization isolates; user routes also filter on the author.
    op.execute(
        f"CREATE POLICY {TABLE}_org_isolation ON {TABLE} "
        "USING (org_id::text = current_setting('app.current_org_id', true)) "
        "WITH CHECK (org_id::text = current_setting('app.current_org_id', true))"
    )


def downgrade() -> None:
    op.drop_table(TABLE)
