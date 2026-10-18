"""Operator-only verified archive manifests and audit claim fences."""

from alembic import op
import sqlalchemy as sa

revision = "20260913_archives"
down_revision = "20260913_archive_control"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE conversation_identities DROP CONSTRAINT conversation_identities_lifecycle_check"
    )
    op.execute(
        "ALTER TABLE conversation_identities ADD CONSTRAINT conversation_identities_lifecycle_check CHECK(lifecycle IN ('active','archiving','archived','restore_pending'))"
    )
    op.execute("""CREATE TABLE archive_jobs (
        id uuid PRIMARY KEY, org_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
        created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
        kind varchar(16) NOT NULL CHECK(kind IN ('conversation','audit')),
        conversation_id uuid,
        FOREIGN KEY(org_id,conversation_id) REFERENCES conversation_identities(org_id,id) ON DELETE RESTRICT,
        generation integer NOT NULL CHECK(generation>0), cutoff timestamptz NOT NULL,
        state varchar(24) NOT NULL CHECK(state IN ('claimed','verified','purged','restore_pending','restored')),
        inventory jsonb NOT NULL, manifest_hash varchar(64),rag jsonb,
        restore_generation integer,staged boolean NOT NULL DEFAULT false,dispatch_checked_at timestamptz,
        CHECK((kind='conversation')=(conversation_id IS NOT NULL)),
        CHECK(state='claimed' OR (manifest_hash IS NOT NULL AND manifest_hash ~ '^[a-f0-9]{64}$'))
    )""")
    op.execute("ALTER TABLE archive_jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE archive_jobs FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON archive_jobs USING(org_id=NULLIF(current_setting('app.current_org_id',true),'')::uuid) WITH CHECK(org_id=NULLIF(current_setting('app.current_org_id',true),'')::uuid)"
    )
    op.execute("REVOKE ALL ON archive_jobs FROM PUBLIC,aurum_ai_app")
    op.execute("""CREATE FUNCTION platform_archive_payload_exists(p_table text,p_id uuid) RETURNS boolean
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
      DECLARE present boolean;
      BEGIN
        IF p_table NOT IN ('conversations','messages','workflow_runs','workflow_node_runs','interaction_turns','interaction_executions','tool_operations','user_questions','interaction_outbox','web_sources','knowledge_bases','skills','file_metadata','audit_logs') THEN
          RAISE EXCEPTION 'Invalid archive table'; END IF;
        EXECUTE format('SELECT EXISTS(SELECT 1 FROM public.%I WHERE id=$1)',p_table) INTO present USING p_id;
        RETURN present;
      END $$""")
    op.execute(
        "REVOKE ALL ON FUNCTION platform_archive_payload_exists(text,uuid) FROM PUBLIC,aurum_ai_app"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION platform_archive_payload_exists(text,uuid) TO aurum_ai_lifecycle"
    )

    op.execute("""CREATE FUNCTION platform_archive_unique_collision(p_table text,p_id uuid,p_values jsonb) RETURNS boolean
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
      DECLARE predicate text; present boolean;
      BEGIN
        predicate := CASE p_table
          WHEN 'knowledge_bases' THEN '(r.org_id=v.org_id AND r.name=v.name) OR r.owning_conversation_id=v.owning_conversation_id'
          WHEN 'skills' THEN 'r.owning_conversation_id=v.owning_conversation_id'
          WHEN 'file_metadata' THEN 'r.storage_key=v.storage_key'
          WHEN 'workflow_runs' THEN 'r.temporal_workflow_id=v.temporal_workflow_id OR (r.conversation_id=v.conversation_id AND r.turn_id=v.turn_id)'
          WHEN 'workflow_node_runs' THEN 'r.workflow_run_id=v.workflow_run_id AND r.phase_key=v.phase_key'
          WHEN 'interaction_turns' THEN 'r.org_id=v.org_id AND r.conversation_id=v.conversation_id AND r.status=''active'' AND v.status=''active'''
          WHEN 'interaction_executions' THEN 'r.org_id=v.org_id AND r.turn_id=v.turn_id AND r.workflow_id=v.workflow_id AND r.run_id=v.run_id'
          WHEN 'tool_operations' THEN 'r.org_id=v.org_id AND r.execution_id=v.execution_id AND r.invocation_id=v.invocation_id'
          WHEN 'user_questions' THEN 'r.org_id=v.org_id AND (r.operation_id=v.operation_id OR (r.execution_id=v.execution_id AND r.status=''pending'' AND v.status=''pending''))'
          WHEN 'interaction_outbox' THEN 'r.org_id=v.org_id AND r.question_id=v.question_id AND r.question_version=v.question_version AND r.event_kind=v.event_kind'
          WHEN 'web_sources' THEN 'r.org_id=v.org_id AND r.operation_id=v.operation_id AND r.position=v.position'
          ELSE NULL END;
        IF predicate IS NULL OR p_id IS NULL OR jsonb_typeof(p_values)<>'object' THEN RAISE EXCEPTION 'Invalid archive unique probe'; END IF;
        EXECUTE format('SELECT EXISTS(SELECT 1 FROM public.%I r CROSS JOIN jsonb_populate_record(NULL::public.%I,$1) v WHERE r.id<>$2 AND (%s))',p_table,p_table,predicate)
          INTO present USING p_values,p_id;
        RETURN present;
      END $$""")
    op.execute(
        "REVOKE ALL ON FUNCTION platform_archive_unique_collision(text,uuid,jsonb) FROM PUBLIC,aurum_ai_app"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION platform_archive_unique_collision(text,uuid,jsonb) TO aurum_ai_lifecycle"
    )

    op.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON archive_jobs TO aurum_ai_lifecycle")
    op.execute("ALTER TABLE conversation_identities ADD COLUMN archive_checked_at timestamptz")
    op.execute(
        "CREATE TABLE archive_audit_dispatch(org_id uuid PRIMARY KEY REFERENCES organizations(id) ON DELETE CASCADE, checked_at timestamptz NOT NULL)"
    )
    op.execute("ALTER TABLE archive_audit_dispatch ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE archive_audit_dispatch FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON archive_audit_dispatch USING(org_id=NULLIF(current_setting('app.current_org_id',true),'')::uuid)"
    )
    op.execute("REVOKE ALL ON archive_audit_dispatch FROM PUBLIC,aurum_ai_app")
    op.execute("GRANT SELECT ON archive_audit_dispatch TO aurum_ai_lifecycle")
    # Discovery reads payload with MVCC, never locks it after the registry.
    # Claims recheck age under the ordinary payload-first locking protocol.
    # Unchecked rows enter at their virtual eligibility time (effective age +
    # configured retention); checked rows reenter at their last check. Thus new
    # age-eligible arrivals cannot keep jumping ahead of blocked candidates.
    op.execute("""CREATE FUNCTION platform_archive_discover(p_limit integer,p_cutoff timestamptz) RETURNS TABLE(org_id uuid,conversation_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off SET timezone='UTC' AS $$
        WITH picked AS (SELECT i.id FROM public.conversation_identities i
          JOIN public.conversations c ON c.id=i.id AND c.org_id=i.org_id AND c.created_at=i.created_at
          WHERE i.lifecycle='active' AND greatest(c.created_at,c.updated_at,c.last_message_at,i.restored_at)<p_cutoff
          ORDER BY coalesce(i.archive_checked_at,greatest(c.created_at,c.updated_at,c.last_message_at,i.restored_at)+(statement_timestamp()-p_cutoff)),i.created_at,i.id LIMIT greatest(1,least(p_limit,1000)) FOR UPDATE OF i SKIP LOCKED)
        UPDATE public.conversation_identities i SET archive_checked_at=clock_timestamp() FROM picked p WHERE i.id=p.id RETURNING i.org_id,i.id $$""")
    op.execute("""CREATE FUNCTION platform_archive_audit_orgs(p_limit integer,p_cutoff timestamptz) RETURNS TABLE(org_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off SET timezone='UTC' AS $$
        WITH picked AS (SELECT a.org_id FROM public.audit_log_identities a
          JOIN public.audit_logs p ON p.id=a.id AND p.org_id=a.org_id AND p.created_at=a.created_at
          LEFT JOIN public.archive_audit_dispatch d ON d.org_id=a.org_id
          WHERE a.lifecycle='active' AND p.created_at<p_cutoff GROUP BY a.org_id,d.checked_at ORDER BY coalesce(d.checked_at,min(p.created_at)+(statement_timestamp()-p_cutoff)),min(a.created_at),a.org_id LIMIT greatest(1,least(p_limit,1000)))
        INSERT INTO public.archive_audit_dispatch(org_id,checked_at) SELECT org_id,clock_timestamp() FROM picked
        ON CONFLICT(org_id) DO UPDATE SET checked_at=excluded.checked_at RETURNING org_id $$""")
    op.execute("REVOKE ALL ON FUNCTION platform_archive_audit_orgs(integer,timestamptz) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION platform_archive_audit_orgs(integer,timestamptz) TO aurum_ai_lifecycle"
    )
    op.execute("REVOKE ALL ON FUNCTION platform_archive_discover(integer,timestamptz) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION platform_archive_discover(integer,timestamptz) TO aurum_ai_lifecycle"
    )
    op.execute("""CREATE FUNCTION platform_archive_pending(p_limit integer) RETURNS TABLE(org_id uuid,archive_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
        WITH picked AS (SELECT id FROM public.archive_jobs WHERE state IN ('claimed','verified')
          ORDER BY dispatch_checked_at NULLS FIRST,created_at,id LIMIT greatest(1,least(p_limit,1000)) FOR UPDATE SKIP LOCKED)
        UPDATE public.archive_jobs j SET dispatch_checked_at=clock_timestamp() FROM picked p WHERE j.id=p.id RETURNING j.org_id,j.id $$""")
    op.execute("REVOKE ALL ON FUNCTION platform_archive_pending(integer) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION platform_archive_pending(integer) TO aurum_ai_lifecycle"
    )
    op.execute("""CREATE FUNCTION platform_guard_audit_archive() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
        DECLARE r public.audit_log_identities%ROWTYPE;
        BEGIN
          IF pg_has_role(current_user,'aurum_ai_lifecycle','MEMBER') AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) THEN
            IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
          END IF;
          IF TG_OP='DELETE' AND ((SELECT rolsuper FROM pg_roles WHERE rolname=current_user) OR (current_user='aurum_ai_partition_owner' AND pg_trigger_depth()>1)) THEN RETURN OLD; END IF;
          SELECT * INTO r FROM public.audit_log_identities WHERE id=OLD.id AND org_id=OLD.org_id FOR UPDATE NOWAIT;
          IF r.lifecycle<>'active' THEN RAISE EXCEPTION 'Audit frozen' USING ERRCODE='55000'; END IF;
          IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
        END $$""")
    op.execute(
        "CREATE TRIGGER archive_audit_fence BEFORE UPDATE OR DELETE ON audit_logs FOR EACH ROW EXECUTE FUNCTION platform_guard_audit_archive()"
    )
    op.execute("""CREATE FUNCTION platform_drop_empty_archive_partitions(p_cutoff timestamptz) RETURNS integer
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET timezone='UTC' AS $$
      DECLARE r record; start_at timestamptz; end_at timestamptz; expected text; occupied boolean; removed integer:=0;
      BEGIN
        IF p_cutoff IS NULL OR p_cutoff>clock_timestamp() THEN RAISE EXCEPTION 'Invalid cutoff'; END IF;
        PERFORM pg_advisory_xact_lock(20260913,2);
        LOCK TABLE public.conversations,public.messages,public.audit_logs IN ACCESS EXCLUSIVE MODE NOWAIT;
        FOR r IN SELECT c.relname,pg_get_expr(c.relpartbound,c.oid) AS bounds,p.relname AS parent
          FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid JOIN pg_class p ON p.oid=i.inhparent
          WHERE p.oid IN ('public.conversations'::regclass,'public.messages'::regclass,'public.audit_logs'::regclass)
          AND c.relnamespace='public'::regnamespace AND c.relowner=p.relowner
          AND c.relname ~ '^(conversations|messages|audit_logs)_p[0-9]{8}$' ORDER BY c.relname LIMIT 100
        LOOP
          start_at:=to_date(right(r.relname,8),'YYYYMMDD')::timestamptz;
          end_at:=start_at+interval '1 month';
          expected:=format('FOR VALUES FROM (%L) TO (%L)',start_at,end_at);
          IF r.bounds<>expected OR end_at>p_cutoff THEN CONTINUE; END IF;
          EXECUTE format('SELECT EXISTS(SELECT 1 FROM public.%I LIMIT 1)',r.relname) INTO occupied;
          IF occupied THEN CONTINUE; END IF;
          EXECUTE format('DROP TABLE public.%I',r.relname);
          removed:=removed+1;
        END LOOP;
        RETURN removed;
      EXCEPTION WHEN lock_not_available THEN RETURN 0;
      END $$""")
    op.execute(
        "ALTER FUNCTION platform_drop_empty_archive_partitions(timestamptz) OWNER TO aurum_ai_partition_owner"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION platform_drop_empty_archive_partitions(timestamptz) FROM PUBLIC,aurum_ai_app"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION platform_drop_empty_archive_partitions(timestamptz) TO aurum_ai_lifecycle"
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM archive_jobs WHERE state<>'restored')")
    ):
        raise RuntimeError("Restore archive jobs before downgrade")
    op.execute("DROP FUNCTION IF EXISTS platform_drop_empty_archive_partitions(timestamptz)")
    op.execute("DROP TRIGGER archive_audit_fence ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS platform_guard_audit_archive()")
    op.execute("DROP FUNCTION IF EXISTS platform_archive_pending(integer)")
    op.execute("DROP FUNCTION IF EXISTS platform_archive_payload_exists(text,uuid)")
    op.execute("DROP FUNCTION IF EXISTS platform_archive_unique_collision(text,uuid,jsonb)")
    op.execute("DROP FUNCTION IF EXISTS platform_archive_discover(integer,timestamptz)")
    op.execute("DROP FUNCTION IF EXISTS platform_archive_audit_orgs(integer,timestamptz)")
    op.execute("ALTER TABLE conversation_identities DROP COLUMN IF EXISTS archive_checked_at")
    op.drop_table("archive_audit_dispatch")
    op.drop_table("archive_jobs")
    op.execute(
        "ALTER TABLE conversation_identities DROP CONSTRAINT conversation_identities_lifecycle_check"
    )
    op.execute(
        "ALTER TABLE conversation_identities ADD CONSTRAINT conversation_identities_lifecycle_check CHECK(lifecycle IN ('active','archiving','archived'))"
    )
