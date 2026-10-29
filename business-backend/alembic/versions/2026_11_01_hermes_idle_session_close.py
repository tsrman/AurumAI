"""Close Hermes sessions left open without a turn.

Revision ID: 20261101_hermes_idle_close
Revises: 20261031_agent_definition_kind

An idle sandbox is retired by its session workflow, which then closes the
session with the conversation runner's token. That token lives thirty
minutes and is renewed only by chat requests, the same as the sandbox's idle
time: the close was refused, and the session stayed open, its token valid,
until its lifetime ended. The backend closes such sessions itself.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20261101_hermes_idle_close"
down_revision: str | None = "20261031_agent_definition_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = "close_idle_hermes_sessions"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION public.{FUNCTION}(p_idle_seconds integer, p_batch_size integer)
        RETURNS integer
        LANGUAGE sql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          WITH clock AS (
            SELECT clock_timestamp() AS now
          ), candidates AS MATERIALIZED (
            SELECT s.id
              FROM public.hermes_sessions AS s
             CROSS JOIN clock
             WHERE public.platform_maintenance_conversation(s.org_id, s.conversation_id)
               AND s.state = 'open'
               AND s.active_turn_id IS NULL
               -- Never sooner than a quarter of an hour without a change.
               AND s.updated_at <= clock.now - make_interval(secs => greatest(coalesce(p_idle_seconds, 0), 900))
             ORDER BY s.updated_at, s.id
             LIMIT greatest(0, least(coalesce(p_batch_size, 0), 1000))
             FOR UPDATE OF s SKIP LOCKED
          ), closed AS (
            -- As closing does: the binding is cleared and every token revoked.
            UPDATE public.hermes_sessions AS s
               SET state = 'closed', closed_at = clock.now, updated_at = clock.now,
                   active_turn_id = NULL, active_execution_id = NULL,
                   workflow_run_id = NULL, workflow_phase = NULL, workflow_node_key = NULL,
                   token_version = s.token_version + 1
              FROM candidates, clock
             WHERE s.id = candidates.id
            RETURNING s.id
          )
          SELECT count(*)::integer FROM closed;
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION public.{FUNCTION}(integer, integer) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.{FUNCTION}(integer, integer) TO aurum_ai_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION public.{FUNCTION}(integer, integer)")
