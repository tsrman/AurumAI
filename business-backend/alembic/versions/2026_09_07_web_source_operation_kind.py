"""Require web source kind to match its durable web operation.

Revision ID: c2e5a8b1d4f7
Revises: b1d4f7a0c3e6
"""

from collections.abc import Sequence

from alembic import op


revision: str = "c2e5a8b1d4f7"
down_revision: str | None = "b1d4f7a0c3e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = "validate_web_source_operation_kind"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION public.{FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1
              FROM public.tool_operations AS operation
             WHERE operation.org_id = NEW.org_id
               AND operation.turn_id = NEW.turn_id
               AND operation.execution_id = NEW.execution_id
               AND operation.id = NEW.operation_id
               AND operation.kind = 'web_' || NEW.kind
          ) THEN
            RAISE EXCEPTION 'web source operation kind mismatch'
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION public.{FUNCTION}() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER trg_web_source_operation_kind BEFORE INSERT ON web_sources "
        f"FOR EACH ROW EXECUTE FUNCTION public.{FUNCTION}()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_web_source_operation_kind ON web_sources")
    op.execute(f"DROP FUNCTION IF EXISTS public.{FUNCTION}()")
