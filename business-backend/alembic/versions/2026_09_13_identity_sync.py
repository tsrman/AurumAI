"""Durable identity admission and bounded dispatcher metadata."""

from alembic import op

revision = "20260913_identity_sync"
down_revision = "78b705d3b523"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE identity_sync_state (
            org_id uuid NOT NULL, user_id uuid NOT NULL,
            requested_generation bigint NOT NULL CHECK (requested_generation > 0),
            applied_generation bigint NOT NULL DEFAULT 0,
            security_generation bigint NOT NULL,
            claims_digest varchar(64) NOT NULL, source_iat bigint NOT NULL,
            status varchar(20) NOT NULL DEFAULT 'pending',
            last_requested_at timestamptz NOT NULL,
            PRIMARY KEY (org_id, user_id),
            CHECK (applied_generation BETWEEN 0 AND requested_generation)
        )
    """)
    op.execute("""
        CREATE TABLE identity_sync_inbox (
            org_id uuid NOT NULL, user_id uuid NOT NULL, generation bigint NOT NULL,
            snapshot jsonb NOT NULL CHECK (octet_length(snapshot::text) <= 65536),
            attempts integer NOT NULL DEFAULT 0,
            error_category varchar(32),
            next_dispatch_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (org_id, user_id, generation)
        )
    """)
    op.execute("CREATE INDEX ix_identity_inbox_dispatch ON identity_sync_inbox(next_dispatch_at)")
    for table in ("identity_sync_state", "identity_sync_inbox"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_org ON {table}
            USING (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
            WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
        """)
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO aurum_ai_app")
    # The only cross-tenant operation: bounded delivery metadata, never snapshots.
    # Owned by the migration role; runtime connections remain FORCE-RLS constrained.
    op.execute("""
        CREATE FUNCTION public.identity_sync_dispatch_batch(batch_size integer)
        RETURNS TABLE(org_id uuid, user_id uuid, generation bigint)
        LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
            SELECT i.org_id, i.user_id, i.generation
            FROM public.identity_sync_inbox i
            WHERE i.next_dispatch_at <= now()
            ORDER BY i.next_dispatch_at, i.org_id, i.user_id, i.generation
            LIMIT LEAST(GREATEST(batch_size, 0), 100)
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.identity_sync_dispatch_batch(integer) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION public.identity_sync_dispatch_batch(integer) TO aurum_ai_app"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION public.identity_sync_dispatch_batch(integer)")
    op.drop_table("identity_sync_inbox")
    op.drop_table("identity_sync_state")
