"""Durable interaction turns, execution identity, questions and delivery outbox.

Revision ID: c6e8a1b4d9f2
Revises: b3d9f2a5c8e1
"""

from collections.abc import Sequence

from alembic import op


revision: str = "c6e8a1b4d9f2"
down_revision: str | None = "b3d9f2a5c8e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "interaction_turns",
    "interaction_executions",
    "tool_operations",
    "user_questions",
    "interaction_outbox",
)
CLAIM_FUNCTION = "claim_interaction_outbox"
ACK_FUNCTION = "ack_interaction_outbox"
PROTECTED_WAITS_FUNCTION = "protected_interaction_waits"

# Frozen DDL: do not import the evolving ORM/schema into a historical migration.
DDL = """
ALTER TABLE conversations ADD CONSTRAINT uq_conversations_org_id_owner UNIQUE (org_id,id,user_id);
CREATE TABLE interaction_turns (
 id uuid PRIMARY KEY, org_id uuid NOT NULL, conversation_id uuid NOT NULL,
 owner_id uuid NOT NULL, workflow_run_id uuid,
 status varchar(16) NOT NULL DEFAULT 'active', version integer NOT NULL DEFAULT 1,
 limits jsonb NOT NULL, question_count integer NOT NULL DEFAULT 0,
 search_count integer NOT NULL DEFAULT 0, read_count integer NOT NULL DEFAULT 0,
 terminal_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT uq_interaction_turn_org_id UNIQUE (org_id,id),
 CONSTRAINT fk_interaction_turn_conversation FOREIGN KEY (org_id,conversation_id,owner_id)
 REFERENCES conversations(org_id,id,user_id) ON DELETE CASCADE,
 CONSTRAINT fk_interaction_turn_owner FOREIGN KEY (org_id,owner_id)
 REFERENCES users(org_id,id) ON DELETE CASCADE,
 CONSTRAINT fk_interaction_turn_workflow_run FOREIGN KEY (org_id,workflow_run_id)
 REFERENCES workflow_runs(org_id,id) ON DELETE RESTRICT,
 CONSTRAINT ck_interaction_turn_status CHECK (status IN ('active','completed','failed','cancelled')),
 CONSTRAINT ck_interaction_turn_version CHECK (version >= 1),
 CONSTRAINT ck_interaction_turn_counters CHECK (question_count >= 0 AND search_count >= 0 AND read_count >= 0),
 CONSTRAINT ck_interaction_turn_limits CHECK (jsonb_typeof(limits) = 'object')
);
CREATE UNIQUE INDEX uq_interaction_active_conversation
 ON interaction_turns(org_id,conversation_id) WHERE status='active';
CREATE TABLE interaction_executions (
 id uuid PRIMARY KEY, org_id uuid NOT NULL, turn_id uuid NOT NULL,
 workflow_id varchar(255) NOT NULL, run_id uuid NOT NULL, task_queue varchar(255) NOT NULL,
 phase varchar(32) NOT NULL, node_key varchar(64), agent_definition_id uuid,
 iteration integer NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT uq_interaction_execution_turn_id UNIQUE (org_id,turn_id,id),
 CONSTRAINT uq_interaction_execution_target UNIQUE (org_id,turn_id,workflow_id,run_id),
 CONSTRAINT fk_interaction_execution_turn FOREIGN KEY (org_id,turn_id)
 REFERENCES interaction_turns(org_id,id) ON DELETE CASCADE,
 CONSTRAINT fk_interaction_execution_agent FOREIGN KEY (org_id,agent_definition_id)
 REFERENCES agent_definitions(org_id,id) ON DELETE RESTRICT,
 CONSTRAINT ck_interaction_execution_address CHECK (length(btrim(workflow_id)) > 0 AND length(btrim(task_queue)) > 0),
 CONSTRAINT ck_interaction_execution_phase CHECK (phase IN ('ordinary','node','supervisor_prepare','supervisor_synthesize')),
 CONSTRAINT ck_interaction_execution_iteration CHECK (iteration >= 1)
);
CREATE TABLE tool_operations (
 id uuid PRIMARY KEY, org_id uuid NOT NULL, turn_id uuid NOT NULL, execution_id uuid NOT NULL,
 invocation_id uuid NOT NULL, kind varchar(16) NOT NULL, arguments_hash varchar(64) NOT NULL,
 status varchar(16) NOT NULL DEFAULT 'reserved', result jsonb, error_code varchar(64),
 attempts integer NOT NULL DEFAULT 0, lease_id uuid, lease_until timestamptz,
 result_expires_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT uq_tool_operation_execution_id UNIQUE (org_id,turn_id,execution_id,id),
 CONSTRAINT uq_tool_operation_invocation UNIQUE (org_id,execution_id,invocation_id),
 CONSTRAINT fk_tool_operation_execution FOREIGN KEY (org_id,turn_id,execution_id)
 REFERENCES interaction_executions(org_id,turn_id,id) ON DELETE CASCADE,
 CONSTRAINT ck_tool_operation_kind CHECK (kind IN ('ask_user','web_search','web_read')),
 CONSTRAINT ck_tool_operation_status CHECK (status IN ('reserved','running','completed','failed','cancelled')),
 CONSTRAINT ck_tool_operation_attempts CHECK (attempts >= 0),
 CONSTRAINT ck_tool_operation_arguments_hash CHECK (arguments_hash ~ '^[0-9a-f]{64}$')
);
CREATE TABLE user_questions (
 id uuid PRIMARY KEY, org_id uuid NOT NULL, turn_id uuid NOT NULL, execution_id uuid NOT NULL,
 operation_id uuid NOT NULL, text text NOT NULL, options jsonb NOT NULL DEFAULT '[]',
 status varchar(16) NOT NULL DEFAULT 'pending', version integer NOT NULL DEFAULT 1,
 expires_at timestamptz NOT NULL, answer jsonb, answered_at timestamptz,
 answered_by uuid, answer_key uuid, consumed_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT uq_user_question_execution_id UNIQUE (org_id,turn_id,execution_id,id),
 CONSTRAINT uq_user_question_operation UNIQUE (org_id,operation_id),
 CONSTRAINT fk_user_question_operation FOREIGN KEY (org_id,turn_id,execution_id,operation_id)
 REFERENCES tool_operations(org_id,turn_id,execution_id,id) ON DELETE CASCADE,
 CONSTRAINT fk_user_question_answerer FOREIGN KEY (org_id,answered_by)
 REFERENCES users(org_id,id) ON DELETE RESTRICT,
 CONSTRAINT ck_user_question_status CHECK (status IN ('pending','answered','expired','cancelled')),
 CONSTRAINT ck_user_question_version CHECK (version >= 1),
 CONSTRAINT ck_user_question_text CHECK (length(text) BETWEEN 1 AND 4000 AND text ~ '[^[:space:]]'),
 CONSTRAINT ck_user_question_consumed CHECK (consumed_at IS NULL OR (status = 'answered' AND consumed_at >= answered_at)),
 CONSTRAINT ck_user_question_options CHECK (jsonb_typeof(options) = 'array' AND jsonb_array_length(options) <= 8),
 CONSTRAINT ck_user_question_deadline CHECK (expires_at > created_at),
 CONSTRAINT ck_user_question_answer_state CHECK (
  (status = 'answered' AND answer IS NOT NULL AND jsonb_typeof(answer) = 'object'
   AND answered_at IS NOT NULL AND answered_by IS NOT NULL AND answer_key IS NOT NULL)
  OR (status <> 'answered' AND answer IS NULL AND answered_at IS NULL
   AND answered_by IS NULL AND answer_key IS NULL))
);
CREATE UNIQUE INDEX uq_question_pending_execution
 ON user_questions(org_id,execution_id) WHERE status='pending';
CREATE INDEX ix_question_pending_deadline ON user_questions(expires_at) WHERE status='pending';
CREATE TABLE interaction_outbox (
 id uuid PRIMARY KEY, org_id uuid NOT NULL, turn_id uuid NOT NULL, execution_id uuid NOT NULL,
 question_id uuid NOT NULL, question_version integer NOT NULL, event_kind varchar(32) NOT NULL,
 status varchar(16) NOT NULL DEFAULT 'pending', attempts integer NOT NULL DEFAULT 0,
 available_at timestamptz NOT NULL DEFAULT now(), lease_id uuid, lease_until timestamptz,
 delivered_at timestamptz, error_code varchar(64),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT fk_interaction_outbox_question FOREIGN KEY (org_id,turn_id,execution_id,question_id)
 REFERENCES user_questions(org_id,turn_id,execution_id,id) ON DELETE CASCADE,
 CONSTRAINT uq_interaction_outbox_event UNIQUE (org_id,question_id,question_version,event_kind),
 CONSTRAINT ck_interaction_outbox_kind CHECK (event_kind IN ('question_status','question_available')),
 CONSTRAINT ck_interaction_outbox_status CHECK (status IN ('pending','delivered','failed','cancelled')),
 CONSTRAINT ck_interaction_outbox_counters CHECK (question_version >= 1 AND attempts >= 0)
);
CREATE INDEX ix_interaction_outbox_pending ON interaction_outbox(available_at) WHERE status='pending';
"""


def upgrade() -> None:
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)
    for table in TABLES:
        op.create_index(f"ix_{table}_org_id", table, ["org_id"])
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO aurum_ai_app")
        op.execute(
            f"CREATE POLICY {table}_org_isolation ON {table} "
            "USING (org_id::text = current_setting('app.current_org_id', true)) "
            "WITH CHECK (org_id::text = current_setting('app.current_org_id', true))"
        )
    op.execute(
        f"""
        CREATE FUNCTION public.{CLAIM_FUNCTION}(
            p_batch_size integer,
            p_lease_seconds integer,
            p_settlement_grace_seconds integer
        )
        RETURNS TABLE (
            outbox_id uuid,
            lease_id uuid,
            workflow_id text,
            run_id uuid,
            question_id uuid,
            question_version integer,
            attempt integer,
            settlement_until timestamptz
        )
        LANGUAGE sql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            WITH clock AS (
                SELECT clock_timestamp() AS now
            ), candidates AS MATERIALIZED (
                SELECT o.id,
                       e.workflow_id::text AS workflow_id,
                       e.run_id,
                       o.question_id,
                       o.question_version,
                       o.attempts + 1 AS attempt,
                       t.status AS turn_status,
                       q.expires_at + make_interval(
                           secs => greatest(1, least(p_settlement_grace_seconds, 86400))
                       ) AS settlement_until,
                       gen_random_uuid() AS new_lease_id,
                       clock.now
                  FROM public.interaction_outbox AS o
                  JOIN public.interaction_executions AS e
                    ON e.org_id = o.org_id
                   AND e.turn_id = o.turn_id
                   AND e.id = o.execution_id
                  JOIN public.user_questions AS q
                    ON q.org_id = o.org_id
                   AND q.turn_id = o.turn_id
                   AND q.execution_id = o.execution_id
                   AND q.id = o.question_id
                  JOIN public.interaction_turns AS t
                    ON t.org_id = o.org_id
                   AND t.id = o.turn_id
                 CROSS JOIN clock
                 WHERE o.event_kind = 'question_available'
                   AND o.status = 'pending'
                   AND o.available_at <= clock.now
                   AND (o.lease_id IS NULL OR o.lease_until <= clock.now)
                 ORDER BY o.available_at, o.id
                 LIMIT greatest(0, least(p_batch_size, 1000))
                 FOR UPDATE OF o SKIP LOCKED
            ), updated AS (
                UPDATE public.interaction_outbox AS o
                   SET status = CASE
                           WHEN c.turn_status <> 'active'
                             OR c.now >= c.settlement_until THEN 'failed'
                           ELSE 'pending'
                       END,
                       attempts = CASE
                           WHEN c.turn_status = 'active'
                             AND c.now < c.settlement_until THEN c.attempt
                           ELSE o.attempts
                       END,
                       lease_id = CASE
                           WHEN c.turn_status = 'active'
                             AND c.now < c.settlement_until THEN c.new_lease_id
                           ELSE NULL
                       END,
                       lease_until = CASE
                           WHEN c.turn_status = 'active'
                             AND c.now < c.settlement_until
                           THEN c.now + make_interval(
                               secs => greatest(1, least(p_lease_seconds, 300))
                           )
                           ELSE NULL
                       END,
                       error_code = CASE
                           WHEN c.turn_status <> 'active'
                             OR c.now >= c.settlement_until
                           THEN 'interaction_delivery_failed'
                           ELSE NULL
                       END,
                       updated_at = c.now
                  FROM candidates AS c
                 WHERE o.id = c.id
                RETURNING o.id,
                          o.lease_id,
                          c.workflow_id,
                          c.run_id,
                          c.question_id,
                          c.question_version,
                          o.attempts,
                          c.settlement_until
            )
            SELECT id,
                   lease_id,
                   workflow_id,
                   run_id,
                   question_id,
                   question_version,
                   attempts,
                   settlement_until
              FROM updated
             WHERE lease_id IS NOT NULL;
        $$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION public.{ACK_FUNCTION}(
            p_outbox_id uuid,
            p_lease_id uuid,
            p_outcome text,
            p_settlement_until timestamptz,
            p_retry_seconds integer
        )
        RETURNS boolean
        LANGUAGE sql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            WITH clock AS (
                SELECT clock_timestamp() AS now
            ), updated AS (
                UPDATE public.interaction_outbox AS o
                   SET status = CASE
                           WHEN p_outcome = 'delivered' THEN 'delivered'
                           WHEN p_outcome = 'failed'
                             OR clock.now >= p_settlement_until
                             OR clock.now + make_interval(
                                 secs => greatest(1, least(p_retry_seconds, 30))
                             ) >= p_settlement_until
                           THEN 'failed'
                           ELSE 'pending'
                       END,
                       delivered_at = CASE
                           WHEN p_outcome = 'delivered' THEN clock.now
                           ELSE o.delivered_at
                       END,
                       error_code = CASE
                           WHEN p_outcome = 'failed'
                             OR clock.now >= p_settlement_until
                             OR (
                                 p_outcome = 'retry'
                                 AND clock.now + make_interval(
                                     secs => greatest(1, least(p_retry_seconds, 30))
                                 ) >= p_settlement_until
                             )
                           THEN 'interaction_delivery_failed'
                           ELSE NULL
                       END,
                       available_at = CASE
                           WHEN p_outcome = 'retry'
                             AND clock.now + make_interval(
                                 secs => greatest(1, least(p_retry_seconds, 30))
                             ) < p_settlement_until
                           THEN clock.now + make_interval(
                               secs => greatest(1, least(p_retry_seconds, 30))
                           )
                           ELSE o.available_at
                       END,
                       lease_id = NULL,
                       lease_until = NULL,
                       updated_at = clock.now
                  FROM clock
                 WHERE o.id = p_outbox_id
                   AND o.lease_id = p_lease_id
                   AND o.status = 'pending'
                   AND p_outcome IN ('delivered', 'retry', 'failed')
                RETURNING o.status
            )
            SELECT coalesce(bool_or(status = 'delivered'), false) FROM updated;
        $$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION public.{PROTECTED_WAITS_FUNCTION}(
            p_conversation_id uuid,
            p_settlement_grace_seconds integer
        )
        RETURNS TABLE (
            org_id uuid,
            conversation_id uuid,
            owner_id uuid,
            question_status text,
            expires_at timestamptz,
            settlement_until timestamptz,
            reconciliation boolean
        )
        LANGUAGE sql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            WITH clock AS (
                SELECT clock_timestamp() AS now
            ), waits AS (
                SELECT t.org_id,
                       t.conversation_id,
                       t.owner_id,
                       q.status::text AS question_status,
                       q.expires_at,
                       CASE
                           WHEN q.status = 'answered'
                           THEN q.answered_at
                           ELSE q.expires_at
                       END + make_interval(
                           secs => greatest(
                               1, least(p_settlement_grace_seconds, 86400)
                           )
                       ) AS settlement_until,
                       (
                           q.status = 'expired'
                           OR (q.status = 'pending' AND clock.now >= q.expires_at)
                       ) AS reconciliation,
                       clock.now
                  FROM public.interaction_turns AS t
                  JOIN public.user_questions AS q
                    ON q.org_id = t.org_id
                   AND q.turn_id = t.id
                 CROSS JOIN clock
                 WHERE t.status = 'active'
                   AND q.consumed_at IS NULL
                   AND (
                       p_conversation_id IS NULL
                       OR t.conversation_id = p_conversation_id
                   )
            )
            SELECT waits.org_id,
                   waits.conversation_id,
                   waits.owner_id,
                   waits.question_status,
                   waits.expires_at,
                   waits.settlement_until,
                   waits.reconciliation
              FROM waits
             WHERE (
                       waits.question_status = 'pending'
                       AND waits.now < waits.expires_at
                   )
                OR (
                       (
                           waits.question_status = 'answered'
                           OR waits.reconciliation
                       )
                       AND waits.now < waits.settlement_until
                   )
             ORDER BY waits.org_id, waits.conversation_id, waits.expires_at;
        $$
        """
    )
    for signature in (
        f"public.{CLAIM_FUNCTION}(integer,integer,integer)",
        f"public.{ACK_FUNCTION}(uuid,uuid,text,timestamptz,integer)",
        f"public.{PROTECTED_WAITS_FUNCTION}(uuid,integer)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO aurum_ai_app")


def downgrade() -> None:
    op.execute(
        f"DROP FUNCTION IF EXISTS public.{PROTECTED_WAITS_FUNCTION}(uuid,integer)"
    )
    op.execute(
        f"DROP FUNCTION IF EXISTS public.{ACK_FUNCTION}(uuid,uuid,text,timestamptz,integer)"
    )
    op.execute(
        f"DROP FUNCTION IF EXISTS public.{CLAIM_FUNCTION}(integer,integer,integer)"
    )
    for table in reversed(TABLES):
        op.drop_table(table)
    op.drop_constraint("uq_conversations_org_id_owner", "conversations", type_="unique")
