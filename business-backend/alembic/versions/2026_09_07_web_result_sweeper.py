"""Add bounded cross-tenant web result scrubber.

Revision ID: a0c3e6f9b2d5
Revises: f9b2d5a8c3e6
"""

from collections.abc import Sequence

from alembic import op


revision: str = "a0c3e6f9b2d5"
down_revision: str | None = "f9b2d5a8c3e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = "scrub_expired_web_results"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION public.{FUNCTION}(p_batch_size integer)
        RETURNS integer
        LANGUAGE sql
        VOLATILE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          WITH clock AS (
            SELECT clock_timestamp() AS now
          ), candidates AS MATERIALIZED (
            SELECT o.id
              FROM public.tool_operations AS o
             CROSS JOIN clock
             WHERE o.kind IN ('web_search','web_read')
               AND o.result IS NOT NULL
               AND o.result_expires_at IS NOT NULL
               AND o.result_expires_at <= clock.now
             ORDER BY o.result_expires_at, o.id
             LIMIT greatest(0, least(p_batch_size, 1000))
             FOR UPDATE OF o SKIP LOCKED
          ), scrubbed_sources AS (
            UPDATE public.web_sources AS s
               SET content = NULL, updated_at = clock.now
              FROM candidates, clock
             WHERE s.operation_id = candidates.id
               AND s.content IS NOT NULL
            RETURNING s.id
          ), scrubbed_operations AS (
            UPDATE public.tool_operations AS o
               SET result = NULL, updated_at = clock.now
              FROM candidates, clock
             WHERE o.id = candidates.id
            RETURNING o.id
          )
          SELECT count(*)::integer FROM scrubbed_operations;
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION public.{FUNCTION}(integer) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.{FUNCTION}(integer) TO aurum_ai_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS public.{FUNCTION}(integer)")
