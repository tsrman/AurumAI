"""Protect immutable web source provenance while allowing content scrubbing.

Revision ID: f9b2d5a8c3e6
Revises: e8a1c4f7b2d5
"""

from collections.abc import Sequence

from alembic import op


revision: str = "f9b2d5a8c3e6"
down_revision: str | None = "e8a1c4f7b2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = "protect_web_source_provenance"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION public.{FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
          IF ROW(
              NEW.id, NEW.org_id, NEW.turn_id, NEW.execution_id, NEW.operation_id,
              NEW.position, NEW.kind, NEW.requested_url, NEW.final_url, NEW.title,
              NEW.content_type, NEW.published_at, NEW.retrieved_at, NEW.truncated,
              NEW.page_count, NEW.created_at
          ) IS DISTINCT FROM ROW(
              OLD.id, OLD.org_id, OLD.turn_id, OLD.execution_id, OLD.operation_id,
              OLD.position, OLD.kind, OLD.requested_url, OLD.final_url, OLD.title,
              OLD.content_type, OLD.published_at, OLD.retrieved_at, OLD.truncated,
              OLD.page_count, OLD.created_at
          ) THEN
            RAISE EXCEPTION 'web source provenance is immutable'
              USING ERRCODE = 'integrity_constraint_violation';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION public.{FUNCTION}() FROM PUBLIC")
    op.execute(
        f"CREATE TRIGGER trg_web_source_provenance BEFORE UPDATE ON web_sources "
        f"FOR EACH ROW EXECUTE FUNCTION public.{FUNCTION}()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_web_source_provenance ON web_sources")
    op.execute(f"DROP FUNCTION IF EXISTS public.{FUNCTION}()")
