"""Files an agent attaches to its answer (CSV, PDF, spreadsheets and the like).

Revision ID: 20261102_conversation_files
Revises: 20261101_hermes_idle_close

Bytes live under the conversation's `conversation-images/` prefix, in its
`files/` folder, so the credentials operators scope to that prefix for
archives and purges reach them too.
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from alembic import op


revision: str = "20261102_conversation_files"
down_revision: str | None = "20261101_hermes_idle_close"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = "'ask_user','web_search','web_read','image_generate','image_edit'"
NEW_KINDS = "'ask_user','web_search','web_read','image_generate','image_edit','file_attach'"
TABLE = "conversation_files"
DDL = """
CREATE TABLE conversation_files (
 id uuid PRIMARY KEY, org_id uuid NOT NULL, conversation_id uuid NOT NULL,
 turn_id uuid NOT NULL, execution_id uuid NOT NULL, operation_id uuid NOT NULL,
 name text NOT NULL, storage_key text NOT NULL, content_type text NOT NULL,
 size_bytes bigint NOT NULL, sha256 char(64) NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT uq_conversation_file_org_id UNIQUE (org_id, id),
 CONSTRAINT uq_conversation_file_storage_key UNIQUE (storage_key),
 CONSTRAINT uq_conversation_file_operation UNIQUE (org_id, operation_id),
 CONSTRAINT fk_conversation_file_conversation FOREIGN KEY (org_id, conversation_id)
   REFERENCES conversation_identities(org_id, id) ON DELETE CASCADE,
 CONSTRAINT fk_conversation_file_operation FOREIGN KEY (org_id, turn_id, execution_id, operation_id)
   REFERENCES tool_operations(org_id, turn_id, execution_id, id) ON DELETE CASCADE,
 CONSTRAINT ck_conversation_file_content_type CHECK (content_type IN (
   'text/csv','text/plain','text/markdown','application/json','application/pdf',
   'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
   'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
   'application/vnd.openxmlformats-officedocument.presentationml.presentation',
   'application/zip')),
 CONSTRAINT ck_conversation_file_size CHECK (size_bytes > 0),
 CONSTRAINT ck_conversation_file_name CHECK (length(name) BETWEEN 1 AND 255),
 CONSTRAINT ck_conversation_file_storage_key CHECK (length(storage_key) BETWEEN 1 AND 1024)
);
CREATE INDEX ix_conversation_files_org_id ON conversation_files(org_id);
CREATE INDEX ix_conversation_files_conversation
 ON conversation_files(org_id, conversation_id, created_at, id);
CREATE INDEX ix_conversation_files_turn ON conversation_files(org_id, turn_id)
"""


_FILE_PREDICATE = """
          WHEN 'conversation_files' THEN 'r.storage_key=v.storage_key OR (r.org_id=v.org_id AND r.operation_id=v.operation_id)'"""


def _archive_probes():  # noqa: ANN202
    """The archive probes as the image revision wrote them, to extend."""
    spec = importlib.util.spec_from_file_location(
        "archive_images", Path(__file__).with_name("2026_10_25_archive_images.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _probes(with_files: bool) -> None:
    images = _archive_probes()
    tables = images._TABLES + ",'conversation_images'" + (",'conversation_files'" if with_files else "")
    predicates = images._PREDICATES + images._IMAGE_PREDICATE + (_FILE_PREDICATE if with_files else "")
    op.execute(images._payload_exists(tables))
    op.execute(images._unique_collision(predicates))
    images._grants()


def _replace(table: str, name: str, condition: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, condition)


def upgrade() -> None:
    _replace("tool_operations", "ck_tool_operation_kind", f"kind IN ({NEW_KINDS})")
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO aurum_ai_app")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO aurum_ai_lifecycle")
    op.execute(
        f"CREATE POLICY {TABLE}_org_isolation ON {TABLE} "
        "USING (org_id::text = current_setting('app.current_org_id', true)) "
        "WITH CHECK (org_id::text = current_setting('app.current_org_id', true))"
    )
    _probes(with_files=True)


def downgrade() -> None:
    _probes(with_files=False)
    op.drop_table(TABLE)
    # Fails instead of deleting data while file operations exist.
    _replace("tool_operations", "ck_tool_operation_kind", f"kind IN ({OLD_KINDS})")
