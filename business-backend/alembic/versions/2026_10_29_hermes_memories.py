"""Hermes Agent memory per user.

Revision ID: 20261029_hermes_memories
Revises: 20261026_hermes_model
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261029_hermes_memories"
down_revision: str | None = "20261026_hermes_model"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "hermes_memories"
DDL = """
CREATE TABLE hermes_memories (
 org_id uuid NOT NULL, user_id uuid NOT NULL, kind text NOT NULL,
 content text NOT NULL DEFAULT '', version integer NOT NULL DEFAULT 1,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (org_id, user_id, kind),
 CONSTRAINT fk_hermes_memory_user FOREIGN KEY (org_id, user_id)
   REFERENCES users(org_id, id) ON DELETE CASCADE,
 CONSTRAINT ck_hermes_memory_kind CHECK (kind IN ('memory','user')),
 CONSTRAINT ck_hermes_memory_size CHECK (
   (kind = 'memory' AND length(content) <= 2200) OR (kind = 'user' AND length(content) <= 1375)),
 CONSTRAINT ck_hermes_memory_version CHECK (version >= 1)
)
"""


def upgrade() -> None:
    op.execute(DDL)
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO aurum_ai_app")
    # The organization isolates; every query also filters on the owning user.
    op.execute(
        f"CREATE POLICY {TABLE}_org_isolation ON {TABLE} "
        "USING (org_id::text = current_setting('app.current_org_id', true)) "
        "WITH CHECK (org_id::text = current_setting('app.current_org_id', true))"
    )


def downgrade() -> None:
    op.drop_table(TABLE)
