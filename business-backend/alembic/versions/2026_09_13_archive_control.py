"""Durable archive fences and exact execution identities.

Revision ID: 20260913_archive_control
Revises: 20260913_partitions
"""

from alembic import op
import sqlalchemy as sa

revision = "20260913_archive_control"
down_revision = "20260913_partitions"
branch_labels = None
depends_on = None

# Table => immutable owner lookup. These expressions consume a JSON row so the
# same trigger works on partition children, and never trusts a caller's GUC bypass.
OWNERS = {
    "conversations": "(r->>'id')::uuid",
    "messages": "(r->>'conversation_id')::uuid",
    "workflow_runs": "(r->>'conversation_id')::uuid",
    "interaction_turns": "(r->>'conversation_id')::uuid",
    "skills": "(r->>'owning_conversation_id')::uuid",
    "knowledge_bases": "(r->>'owning_conversation_id')::uuid",
    "workflow_node_runs": "(SELECT conversation_id FROM public.workflow_runs WHERE id=(r->>'workflow_run_id')::uuid AND org_id=o)",
    **{
        table: "(SELECT conversation_id FROM public.interaction_turns WHERE id=(r->>'turn_id')::uuid AND org_id=o)"
        for table in (
            "interaction_executions",
            "tool_operations",
            "user_questions",
            "interaction_outbox",
            "web_sources",
        )
    },
    "file_metadata": "COALESCE((SELECT k.owning_conversation_id FROM public.knowledge_bases k WHERE k.org_id=o AND r->>'storage_key' LIKE 'documents/'||k.org_id::text||'/'||k.id::text||'/%'),(r->>'owning_conversation_id')::uuid)",
    "conversation_operations": "(r->>'conversation_id')::uuid",
    "conversation_executions": "(r->>'conversation_id')::uuid",
}


MAINTENANCE_PATCHES = {
    "claim_interaction_outbox": (
        "WHERE o.event_kind = 'question_available'",
        "WHERE public.platform_maintenance_conversation(t.org_id,t.conversation_id) AND o.event_kind = 'question_available'",
    ),
    "ack_interaction_outbox": (
        "WHERE o.id = p_outbox_id",
        "WHERE public.platform_maintenance_conversation(o.org_id,(SELECT conversation_id FROM public.interaction_turns WHERE id=o.turn_id AND org_id=o.org_id)) AND o.id = p_outbox_id",
    ),
    "scrub_expired_web_results": (
        "WHERE o.kind IN ('web_search','web_read')",
        "WHERE public.platform_maintenance_conversation(o.org_id,(SELECT conversation_id FROM public.interaction_turns WHERE id=o.turn_id AND org_id=o.org_id)) AND o.kind IN ('web_search','web_read')",
    ),
    "clear_expired_workflow_diagnostics": (
        "WHERE diagnostic_expires_at IS NOT NULL",
        "WHERE public.platform_maintenance_conversation(org_id,(SELECT conversation_id FROM public.workflow_runs WHERE id=workflow_node_runs.workflow_run_id AND org_id=workflow_node_runs.org_id)) AND diagnostic_expires_at IS NOT NULL",
    ),
}


def upgrade():
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='aurum_ai_lifecycle') THEN CREATE ROLE aurum_ai_lifecycle NOLOGIN; END IF; END $$"
    )
    op.add_column("conversation_identities", sa.Column("restored_at", sa.DateTime(timezone=True)))
    op.add_column(
        "conversation_identities",
        sa.Column(
            "execution_provenance_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.alter_column(
        "conversation_identities", "execution_provenance_complete", server_default=sa.text("true")
    )
    op.add_column("file_metadata", sa.Column("owning_conversation_id", sa.UUID()))
    op.execute(
        """UPDATE file_metadata f SET owning_conversation_id=k.owning_conversation_id FROM knowledge_bases k WHERE k.org_id=f.org_id AND k.owning_conversation_id IS NOT NULL AND f.storage_key LIKE 'documents/'||k.org_id::text||'/'||k.id::text||'/%'"""
    )
    op.create_foreign_key(
        "fk_file_conversation_identity",
        "file_metadata",
        "conversation_identities",
        ["org_id", "owning_conversation_id"],
        ["org_id", "id"],
        ondelete="RESTRICT",
    )
    op.execute("""CREATE TABLE conversation_operations (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), org_id uuid NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      conversation_id uuid NOT NULL, generation bigint NOT NULL CHECK(generation>0),
      kind varchar(32) NOT NULL, operation_key uuid NOT NULL,
      status varchar(16) NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','settled')),
      workflow_id varchar(255), run_id uuid, knowledge_base_id uuid, resource_key varchar(1024), settled_at timestamptz,
      UNIQUE(org_id,conversation_id,generation,kind,operation_key),
      FOREIGN KEY(org_id,conversation_id) REFERENCES conversation_identities(org_id,id) ON DELETE RESTRICT
    )""")
    op.execute("""CREATE TABLE conversation_executions (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(), org_id uuid NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      conversation_id uuid NOT NULL, generation bigint NOT NULL CHECK(generation>0),
      namespace varchar(255) NOT NULL, workflow_id varchar(255) NOT NULL, run_id uuid NOT NULL,
      workflow_type varchar(64) NOT NULL, task_queue varchar(255) NOT NULL,
      parent_workflow_id varchar(255), parent_run_id uuid, initiated_event_id bigint,
      terminal_status varchar(32), closed_at timestamptz, observed_at timestamptz,
      tree_complete boolean NOT NULL DEFAULT false, blocked_reason varchar(64),
      UNIQUE(namespace,workflow_id,run_id),
      CHECK(NOT tree_complete OR (terminal_status IS NOT NULL AND closed_at IS NOT NULL AND observed_at IS NOT NULL)),
      FOREIGN KEY(org_id,conversation_id) REFERENCES conversation_identities(org_id,id) ON DELETE RESTRICT
    )""")
    op.create_index("ix_conversation_operations_org_id", "conversation_operations", ["org_id"])
    op.create_index("ix_conversation_executions_org_id", "conversation_executions", ["org_id"])
    for table in ("conversation_operations", "conversation_executions"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING(org_id=NULLIF(current_setting('app.current_org_id',true),'')::uuid) WITH CHECK(org_id=NULLIF(current_setting('app.current_org_id',true),'')::uuid)"
        )
        op.execute(f"REVOKE ALL ON {table} FROM aurum_ai_app")
        op.execute(f"GRANT SELECT ON {table} TO aurum_ai_app")
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO aurum_ai_lifecycle")
    op.execute("GRANT INSERT,UPDATE ON conversation_operations TO aurum_ai_app")
    op.execute(
        "GRANT INSERT(id,org_id,conversation_id,generation,namespace,workflow_id,run_id,workflow_type,task_queue,parent_workflow_id,parent_run_id,initiated_event_id) ON conversation_executions TO aurum_ai_app"
    )
    op.execute("GRANT USAGE ON SCHEMA public TO aurum_ai_lifecycle")
    op.execute(
        "GRANT EXECUTE ON FUNCTION platform_ensure_partition(text,timestamptz),platform_maintain_partitions() TO aurum_ai_lifecycle"
    )
    op.execute(
        "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO aurum_ai_lifecycle"
    )
    op.execute("GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO aurum_ai_lifecycle")
    op.execute("""CREATE FUNCTION platform_require_active_registry(p_org uuid,p_id uuid) RETURNS bigint
    LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
    DECLARE state text; fence bigint;
    BEGIN
      IF row_security_active('public.conversation_identities'::regclass) AND p_org IS DISTINCT FROM NULLIF(current_setting('app.current_org_id',true),'')::uuid THEN
        RAISE EXCEPTION 'Tenant unavailable' USING ERRCODE='42501';
      END IF;
      SELECT lifecycle,generation INTO state,fence FROM public.conversation_identities WHERE id=p_id AND org_id=p_org FOR UPDATE NOWAIT;
      IF NOT FOUND THEN RAISE EXCEPTION 'Live conversation unavailable' USING ERRCODE='23503'; END IF;
      IF state <> 'active' THEN RAISE EXCEPTION 'Conversation frozen' USING ERRCODE='55000'; END IF;
      RETURN fence;
    EXCEPTION WHEN lock_not_available THEN
      RAISE EXCEPTION 'Concurrent lifecycle transition' USING ERRCODE='55000';
    END $$""")
    for table, expression in OWNERS.items():
        op.execute(f"""CREATE FUNCTION platform_fence_{table}() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
        DECLARE r jsonb; o uuid; c uuid; old_c uuid; new_r jsonb;
        BEGIN
          IF (pg_has_role(current_user,'aurum_ai_lifecycle','MEMBER') AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user)) OR (TG_OP='DELETE' AND ((SELECT rolsuper FROM pg_roles WHERE rolname=current_user) OR (current_user='aurum_ai_partition_owner' AND pg_trigger_depth()>1))) THEN
            IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
          END IF;
          IF TG_OP='DELETE' THEN r:=to_jsonb(OLD); ELSE r:=to_jsonb(NEW); END IF;
          o:=(r->>'org_id')::uuid; c:={expression};
          IF TG_OP='INSERT' AND '{table}'='file_metadata' THEN
            IF (r->>'owning_conversation_id') IS NOT NULL AND (r->>'owning_conversation_id')::uuid IS DISTINCT FROM c THEN
              RAISE EXCEPTION 'Attachment owner mismatch' USING ERRCODE='23514';
            END IF;
            NEW:=jsonb_populate_record(NEW,jsonb_build_object('owning_conversation_id',c));
          END IF;
          IF TG_OP='UPDATE' THEN
            IF '{table}'='file_metadata' AND (
              (to_jsonb(OLD)->>'owning_conversation_id') IS DISTINCT FROM (r->>'owning_conversation_id')
              OR ((to_jsonb(OLD)->>'owning_conversation_id') IS NOT NULL
                  AND (to_jsonb(OLD)->>'storage_key') IS DISTINCT FROM (r->>'storage_key'))
              OR (r->>'owning_conversation_id')::uuid IS DISTINCT FROM c
            ) THEN
              RAISE EXCEPTION 'Immutable attachment source identity' USING ERRCODE='23514';
            END IF;
            new_r:=r; r:=to_jsonb(OLD); old_c:={expression}; r:=new_r;
            IF old_c IS DISTINCT FROM c AND old_c IS NOT NULL THEN
              PERFORM public.platform_require_active_registry(OLD.org_id,old_c);
              RAISE EXCEPTION 'Immutable conversation reference' USING ERRCODE='23503';
            END IF;
          END IF;
          IF c IS NULL THEN IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF; END IF;
          -- An existing child's implicit tuple lock must not reacquire parent.
          -- App paths acquire the parent first; trigger UPDATE/DELETE backstops
          -- use NOWAIT on registry so claim can safely scrub locked children.
          IF TG_OP='INSERT' AND '{table}' NOT IN ('conversations') THEN
            PERFORM public.platform_lock_live_conversation(o,c);
            IF '{table}'='workflow_runs' THEN
              PERFORM public.platform_lock_live_message(o,(r->>'source_message_id')::uuid,c);
            END IF;
          END IF;
          IF TG_OP<>'INSERT' OR '{table}'<>'conversations' THEN
            PERFORM public.platform_require_active_registry(o,c);
          END IF;
          IF TG_OP='DELETE' AND '{table}'='knowledge_bases' AND EXISTS(
            SELECT 1 FROM public.conversation_operations
            WHERE org_id=o AND knowledge_base_id=(r->>'id')::uuid AND status<>'settled'
          ) THEN
            RAISE EXCEPTION 'Unsettled attachment operation' USING ERRCODE='55000';
          END IF;
          IF TG_OP='UPDATE' AND ((to_jsonb(OLD)->>'org_id') IS DISTINCT FROM (r->>'org_id')) THEN
            RAISE EXCEPTION 'Immutable lifecycle owner' USING ERRCODE='23514';
          END IF;
          IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
        END $$""")
        op.execute(
            f"CREATE TRIGGER archive_fence BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION platform_fence_{table}()"
        )
    # Restore is an operator-only identity match. Retain all immutable identity
    # checks in existing functions; relax active-only admission solely for role.
    for name in (
        "platform_identity_conversations",
        "platform_identity_messages",
        "platform_identity_workflow_runs",
        "platform_identity_audit_logs",
        "platform_live_conversation_reference",
        "platform_lock_live_conversation",
        "platform_lock_live_message",
    ):
        definition = (
            op.get_bind()
            .execute(
                sa.text("SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname=:name"),
                {"name": name},
            )
            .scalar_one()
        )
        definition = definition.replace(
            "lifecycle='active'",
            "(lifecycle='active' OR (pg_has_role(current_user,'aurum_ai_lifecycle','MEMBER') AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user)))",
        )
        definition = definition.replace("r.(lifecycle", "(r.lifecycle")
        if name == "platform_lock_live_conversation":
            definition = definition.replace(
                " AND (lifecycle='active' OR (pg_has_role(current_user,'aurum_ai_lifecycle','MEMBER') AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user)))",
                "",
            )
        op.execute(definition)

    install_maintenance_fences()


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM conversation_identities WHERE lifecycle<>'active')"
            )
        )
        .scalar_one()
    ):
        raise RuntimeError("Restore archived conversations before lifecycle downgrade")
    for name, (old, new) in MAINTENANCE_PATCHES.items():
        definition = (
            op.get_bind()
            .execute(
                sa.text("SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname=:name"),
                {"name": name},
            )
            .scalar_one()
        )
        op.execute(definition.replace(new, old))
    for routine in (
        "cancel_terminal_question_status(integer)",
        "discover_pending_conversation_executions(integer)",
        "discover_pending_conversation_operations(integer)",
        "platform_maintenance_conversation(uuid,uuid)",
    ):
        op.execute(f"DROP FUNCTION {routine}")
    for name in (
        "platform_identity_conversations",
        "platform_identity_messages",
        "platform_identity_workflow_runs",
        "platform_identity_audit_logs",
        "platform_live_conversation_reference",
        "platform_lock_live_conversation",
        "platform_lock_live_message",
    ):
        definition = (
            op.get_bind()
            .execute(
                sa.text("SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname=:name"),
                {"name": name},
            )
            .scalar_one()
        )
        definition = definition.replace(
            "(r.lifecycle='active' OR (pg_has_role(current_user,'aurum_ai_lifecycle','MEMBER') AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user)))",
            "r.lifecycle='active'",
        )
        definition = definition.replace(
            "(lifecycle='active' OR (pg_has_role(current_user,'aurum_ai_lifecycle','MEMBER') AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user)))",
            "lifecycle='active'",
        )
        if name == "platform_lock_live_conversation":
            definition = definition.replace(
                "WHERE id=p_id AND org_id=p_org;",
                "WHERE id=p_id AND org_id=p_org AND lifecycle='active';",
            )
        op.execute(definition)
    for table in reversed(OWNERS):
        op.execute(f"DROP TRIGGER IF EXISTS archive_fence ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS platform_fence_{table}()")
    op.execute("DROP FUNCTION platform_require_active_registry(uuid,uuid)")
    op.drop_table("conversation_executions")
    op.drop_table("conversation_operations")
    op.drop_constraint("fk_file_conversation_identity", "file_metadata", type_="foreignkey")
    op.drop_column("file_metadata", "owning_conversation_id")
    op.drop_column("conversation_identities", "restored_at")
    op.execute("ALTER TABLE conversation_identities DROP COLUMN IF EXISTS execution_provenance_complete")


def install_maintenance_fences():
    op.execute("""CREATE FUNCTION platform_maintenance_conversation(p_org uuid,p_id uuid) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
    DECLARE at timestamptz; state text;
    BEGIN
      SELECT created_at INTO at FROM public.conversation_identities WHERE org_id=p_org AND id=p_id;
      PERFORM 1 FROM public.conversations WHERE org_id=p_org AND id=p_id AND created_at=at FOR NO KEY UPDATE SKIP LOCKED;
      IF NOT FOUND THEN RETURN false; END IF;
      SELECT lifecycle INTO state FROM public.conversation_identities WHERE org_id=p_org AND id=p_id FOR UPDATE;
      RETURN state='active';
    END $$""")
    op.execute("REVOKE ALL ON FUNCTION platform_maintenance_conversation(uuid,uuid) FROM PUBLIC")
    for name, (old, new) in MAINTENANCE_PATCHES.items():
        definition = (
            op.get_bind()
            .execute(
                sa.text("SELECT pg_get_functiondef(oid) FROM pg_proc WHERE proname=:name"),
                {"name": name},
            )
            .scalar_one()
        )
        if old not in definition:
            raise RuntimeError("Maintenance routine contract changed")
        op.execute(definition.replace(old, new))
    op.execute("""CREATE FUNCTION cancel_terminal_question_status(p_limit integer) RETURNS integer
    LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
    WITH candidates AS MATERIALIZED (
      SELECT o.id FROM public.interaction_outbox o JOIN public.interaction_turns t ON t.id=o.turn_id AND t.org_id=o.org_id
      WHERE o.event_kind='question_status' AND o.status='pending' AND t.status<>'active'
        AND public.platform_maintenance_conversation(t.org_id,t.conversation_id)
      ORDER BY o.id LIMIT greatest(0,least(coalesce(p_limit,0),1000)) FOR UPDATE OF o SKIP LOCKED
    ), cancelled AS (
      UPDATE public.interaction_outbox o SET status='cancelled',error_code='terminal_turn_obsolete',lease_id=NULL,lease_until=NULL,updated_at=clock_timestamp()
      FROM candidates c WHERE o.id=c.id RETURNING o.id
    ) SELECT count(*)::integer FROM cancelled $$""")
    op.execute("REVOKE ALL ON FUNCTION cancel_terminal_question_status(integer) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION cancel_terminal_question_status(integer) TO aurum_ai_app,aurum_ai_lifecycle"
    )
    op.execute("""CREATE FUNCTION discover_pending_conversation_executions(p_limit integer) RETURNS TABLE(org_id uuid,id uuid)
    LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
      SELECT e.org_id,e.id FROM public.conversation_executions e WHERE NOT e.tree_complete AND e.parent_run_id IS NULL
      ORDER BY e.observed_at NULLS FIRST,e.id LIMIT greatest(0,least(coalesce(p_limit,0),1000))
    $$""")
    op.execute(
        "REVOKE ALL ON FUNCTION discover_pending_conversation_executions(integer) FROM PUBLIC,aurum_ai_app"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION discover_pending_conversation_executions(integer) TO aurum_ai_lifecycle"
    )

    op.execute("""CREATE FUNCTION discover_pending_conversation_operations(p_limit integer) RETURNS TABLE(org_id uuid,id uuid)
    LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
      SELECT o.org_id,o.id FROM public.conversation_operations o WHERE o.status='pending' AND o.kind IN ('workflow_signal','workflow_start')
      ORDER BY o.updated_at,o.id LIMIT greatest(0,least(coalesce(p_limit,0),1000))
    $$""")
    op.execute(
        "REVOKE ALL ON FUNCTION discover_pending_conversation_operations(integer) FROM PUBLIC,aurum_ai_app"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION discover_pending_conversation_operations(integer) TO aurum_ai_lifecycle"
    )
