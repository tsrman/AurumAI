"""Make a NULL web-result scrub batch fail closed.

Revision ID: b1d4f7a0c3e6
Revises: a0c3e6f9b2d5
"""

from collections.abc import Sequence

from alembic import op


revision: str = "b1d4f7a0c3e6"
down_revision: str | None = "a0c3e6f9b2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = "scrub_expired_web_results"


def _replace(batch_limit: str) -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.{FUNCTION}(p_batch_size integer)
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
             LIMIT {batch_limit}
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


def upgrade() -> None:
    _replace("greatest(0, least(coalesce(p_batch_size, 0), 1000))")


def downgrade() -> None:
    _replace("greatest(0, least(p_batch_size, 1000))")
