"""Previews of the apps Hermes Agent runs in its sandbox.

Revision ID: 20261105_hermes_previews
Revises: 20261104_upload_images

A preview names a port of a session's sandbox, shared with the conversation's
owner for its turn; a ticket is the one-time pass the owner's browser
exchanges for the preview's own cookie. Both are runtime state of the session
and go with it.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261105_hermes_previews"
down_revision: str | None = "20261104_upload_images"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE TABLE hermes_previews (
 id uuid PRIMARY KEY, org_id uuid NOT NULL, session_id uuid NOT NULL,
 conversation_id uuid NOT NULL, owner_id uuid NOT NULL, turn_id uuid NOT NULL,
 port integer NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT uq_hermes_preview_org_id UNIQUE (org_id, id),
 CONSTRAINT uq_hermes_preview_session_port UNIQUE (org_id, session_id, port),
 CONSTRAINT fk_hermes_preview_session FOREIGN KEY (org_id, session_id)
   REFERENCES hermes_sessions(org_id, id) ON DELETE CASCADE,
 CONSTRAINT fk_hermes_preview_turn FOREIGN KEY (org_id, turn_id)
   REFERENCES interaction_turns(org_id, id) ON DELETE CASCADE,
 CONSTRAINT ck_hermes_preview_port CHECK (port BETWEEN 1024 AND 65535)
);
CREATE INDEX ix_hermes_previews_org_id ON hermes_previews(org_id);
CREATE INDEX ix_hermes_previews_turn ON hermes_previews(org_id, turn_id);
CREATE TABLE hermes_preview_tickets (
 id uuid PRIMARY KEY, org_id uuid NOT NULL, preview_id uuid NOT NULL,
 expires_at timestamptz NOT NULL, used_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT fk_hermes_preview_ticket_preview FOREIGN KEY (org_id, preview_id)
   REFERENCES hermes_previews(org_id, id) ON DELETE CASCADE
);
CREATE INDEX ix_hermes_preview_tickets_org_id ON hermes_preview_tickets(org_id);
CREATE INDEX ix_hermes_preview_tickets_expiry ON hermes_preview_tickets(expires_at)
"""
TABLES = ("hermes_previews", "hermes_preview_tickets")


def upgrade() -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO aurum_ai_app")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO aurum_ai_lifecycle")
        op.execute(
            f"CREATE POLICY {table}_org_isolation ON {table} "
            "USING (org_id::text = current_setting('app.current_org_id', true)) "
            "WITH CHECK (org_id::text = current_setting('app.current_org_id', true))"
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
