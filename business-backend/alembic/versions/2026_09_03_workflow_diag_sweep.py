"""give the retention reaper an RLS-safe cross-tenant sweep

Revision ID: 2026_09_03_workflow_diag_sweep
Revises: 2026_09_02_multi_agent_workflows
Create Date: 2026-09-03 10:00:00.000000

`workflow_node_runs` carries FORCE ROW LEVEL SECURITY with the same
`org_id::text = current_setting('app.current_org_id', true)` predicate as every
other org-owned table, so the background retention sweep — which is inherently
cross-tenant and therefore has no single org context to set — matched zero rows
under the `aurum_ai_app` runtime role and silently retained expired
diagnostics forever.

Handing the running backend the migration role's credentials would have fixed
it by weakening the very posture the RLS design exists for (and Helm
deliberately does not give the backend `APP_MIGRATION_DATABASE_URL` at all).
Instead the sweep moves into a SECURITY DEFINER function owned by the migration
role: it runs with the definer's rights, so it sees every tenant's rows, but it
takes only a cutoff timestamp, has a pinned search_path, returns nothing but a
count, and can do exactly one thing — null out two already-expired diagnostic
columns. EXECUTE is revoked from PUBLIC and granted only to the runtime role.
"""

from collections.abc import Sequence

from alembic import op


revision: str = "2026_09_03_workflow_diag_sweep"
down_revision: str | None = "2026_09_02_multi_agent_workflows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


FUNCTION_NAME = "clear_expired_workflow_diagnostics"
ROLE_NAME = "aurum_ai_app"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.{FUNCTION_NAME}(cutoff timestamptz)
        RETURNS integer
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            WITH swept AS (
                UPDATE public.workflow_node_runs
                   SET output = NULL,
                       internal_error = NULL,
                       updated_at = now()
                 WHERE diagnostic_expires_at IS NOT NULL
                   AND diagnostic_expires_at <= cutoff
                   AND (output IS NOT NULL OR internal_error IS NOT NULL)
                RETURNING 1
            )
            SELECT count(*)::integer FROM swept;
        $$
        """
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION public.{FUNCTION_NAME}(timestamptz) FROM PUBLIC"
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE_NAME}') THEN
                EXECUTE 'GRANT EXECUTE ON FUNCTION '
                    || 'public.{FUNCTION_NAME}(timestamptz) TO {ROLE_NAME}';
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS public.{FUNCTION_NAME}(timestamptz)")
