"""Hermes Agent sessions.

Revision ID: 20261025_hermes_sessions
Revises: 20261025_hermes_capability
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261025_hermes_sessions"
down_revision: str | None = "20261025_hermes_capability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "hermes_sessions"
DDL = """
CREATE TABLE hermes_sessions (
 id uuid PRIMARY KEY, org_id uuid NOT NULL, user_id uuid NOT NULL, conversation_id uuid NOT NULL,
 state text NOT NULL DEFAULT 'open', token_version integer NOT NULL DEFAULT 1,
 active_turn_id uuid, workflow_run_id uuid, workflow_phase text, workflow_node_key text,
 expires_at timestamptz NOT NULL, closed_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT uq_hermes_session_org_id UNIQUE (org_id, id),
 CONSTRAINT fk_hermes_session_conversation FOREIGN KEY (org_id, conversation_id, user_id)
   REFERENCES conversation_identities(org_id, id, user_id) ON DELETE CASCADE,
 CONSTRAINT fk_hermes_session_turn FOREIGN KEY (org_id, active_turn_id)
   REFERENCES interaction_turns(org_id, id),
 CONSTRAINT fk_hermes_session_workflow_run FOREIGN KEY (org_id, workflow_run_id)
   REFERENCES workflow_runs(org_id, id),
 CONSTRAINT ck_hermes_session_state CHECK (state IN ('open','closed')),
 CONSTRAINT ck_hermes_session_token_version CHECK (token_version >= 1),
 CONSTRAINT ck_hermes_session_closed CHECK (
   (state = 'open' AND closed_at IS NULL) OR (state = 'closed' AND closed_at IS NOT NULL
     AND active_turn_id IS NULL AND workflow_run_id IS NULL)),
 CONSTRAINT ck_hermes_session_correlation_turn CHECK (
   active_turn_id IS NOT NULL OR workflow_run_id IS NULL),
 CONSTRAINT ck_hermes_session_correlation CHECK (
   (workflow_run_id IS NULL AND workflow_phase IS NULL AND workflow_node_key IS NULL) OR
   (workflow_run_id IS NOT NULL AND (
     (workflow_phase = 'node' AND workflow_node_key ~ '^[a-z][a-z0-9_-]{0,63}$') OR
     (workflow_phase IN ('supervisor_prepare','supervisor_synthesize')
       AND workflow_node_key IS NULL))))
);
CREATE INDEX ix_hermes_sessions_org_id ON hermes_sessions(org_id);
CREATE UNIQUE INDEX uq_hermes_session_open_conversation
 ON hermes_sessions(org_id, conversation_id) WHERE state = 'open';
CREATE INDEX ix_hermes_sessions_turn ON hermes_sessions(org_id, active_turn_id)
 WHERE active_turn_id IS NOT NULL;
CREATE INDEX ix_hermes_sessions_workflow_run ON hermes_sessions(org_id, workflow_run_id)
 WHERE workflow_run_id IS NOT NULL
"""


def upgrade() -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO aurum_ai_app")
    # The lifecycle worker reads every conversation-scoped table, and the
    # archive purge removes a conversation's closed sessions; its blanket grant
    # only covered tables that existed when it was created.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO aurum_ai_lifecycle")
    op.execute(
        f"CREATE POLICY {TABLE}_org_isolation ON {TABLE} "
        "USING (org_id::text = current_setting('app.current_org_id', true)) "
        "WITH CHECK (org_id::text = current_setting('app.current_org_id', true))"
    )


def downgrade() -> None:
    op.drop_table(TABLE)
