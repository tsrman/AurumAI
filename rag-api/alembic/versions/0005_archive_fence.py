"""Separate runtime privileges and durable RAG lifecycle fencing."""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    db = op.get_bind()
    if db.scalar(sa.text('SELECT current_user')) == 'rag_app':
        raise RuntimeError('rag_migration_operator_required')
    op.execute("""DO $$ BEGIN
      IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='rag_schema_owner') THEN
        CREATE ROLE rag_schema_owner NOLOGIN;
      END IF;
      IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='rag_lifecycle') THEN
        CREATE ROLE rag_lifecycle NOLOGIN;
      END IF;
      IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='rag_app') THEN
        CREATE ROLE rag_app NOLOGIN;
      END IF;
      EXECUTE format('ALTER DATABASE %I OWNER TO rag_schema_owner',current_database());
    END $$""")
    op.execute('ALTER SCHEMA public OWNER TO rag_schema_owner')
    op.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC, rag_app')
    op.execute('GRANT USAGE ON SCHEMA public TO rag_app, rag_lifecycle')
    op.execute('ALTER TABLE documents OWNER TO rag_schema_owner')
    op.execute('REVOKE ALL ON documents FROM rag_app')
    op.execute('GRANT SELECT, INSERT, UPDATE, DELETE ON documents TO rag_app, rag_lifecycle')
    op.execute("""CREATE TABLE rag_kb_lifecycle (
      knowledge_base_id uuid PRIMARY KEY, org_id uuid NOT NULL,
      state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','archiving','archived','restore_pending')),
      generation bigint NOT NULL DEFAULT 1 CHECK(generation>0), archive_id uuid,
      provenance_complete boolean NOT NULL DEFAULT true,
      UNIQUE(org_id,knowledge_base_id))""")
    op.execute("""INSERT INTO rag_kb_lifecycle(knowledge_base_id,org_id,provenance_complete)
      SELECT DISTINCT knowledge_base_id,org_id,false FROM documents""")
    op.execute("""CREATE TABLE rag_archive_jobs (
      archive_id uuid PRIMARY KEY, org_id uuid NOT NULL, knowledge_base_id uuid NOT NULL,
      generation bigint NOT NULL, restore_generation bigint, state text NOT NULL DEFAULT 'claimed',
      manifest jsonb, receipt_hash text, inventory jsonb NOT NULL DEFAULT '[]',
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY(org_id,knowledge_base_id) REFERENCES rag_kb_lifecycle(org_id,knowledge_base_id))""")
    op.execute("""CREATE TABLE rag_index_executions (
      operation_id uuid PRIMARY KEY, org_id uuid NOT NULL, knowledge_base_id uuid NOT NULL,
      document_id uuid NOT NULL, generation bigint NOT NULL,
      workflow_id text NOT NULL UNIQUE, run_id uuid, task_queue text NOT NULL,
      terminal_status text, closed_at timestamptz,
      FOREIGN KEY(org_id,knowledge_base_id) REFERENCES rag_kb_lifecycle(org_id,knowledge_base_id))""")
    op.execute("""CREATE TABLE rag_external_operations (
      operation_id uuid PRIMARY KEY, org_id uuid NOT NULL, knowledge_base_id uuid NOT NULL,
      document_id uuid NOT NULL, generation bigint NOT NULL,
      kind text NOT NULL CHECK(kind IN ('vector_upsert','vector_delete')),
      settled boolean NOT NULL DEFAULT false,
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY(org_id,knowledge_base_id) REFERENCES rag_kb_lifecycle(org_id,knowledge_base_id))""")
    op.execute("""CREATE TABLE rag_archive_steps (
      archive_id uuid NOT NULL REFERENCES rag_archive_jobs(archive_id),
      org_id uuid NOT NULL, step text NOT NULL, resource text NOT NULL,
      state text NOT NULL CHECK(state IN ('intent','done')),
      PRIMARY KEY(archive_id,step,resource))""")
    for table in ('rag_kb_lifecycle','rag_archive_jobs','rag_index_executions',
                  'rag_external_operations','rag_archive_steps'):
        op.execute(f'ALTER TABLE {table} OWNER TO rag_schema_owner')
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute(f"CREATE POLICY tenant ON {table} USING (org_id = nullif(current_setting('app.current_org_id',true),'')::uuid) WITH CHECK (org_id = nullif(current_setting('app.current_org_id',true),'')::uuid)")
        op.execute(f'GRANT SELECT ON {table} TO rag_app')
        op.execute(f'GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO rag_lifecycle')
    op.execute("""CREATE FUNCTION rag_writer_fence(o uuid,k uuid,g bigint DEFAULT NULL)
      RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE r public.rag_kb_lifecycle; BEGIN
        PERFORM set_config('app.current_org_id',o::text,true);
        INSERT INTO public.rag_kb_lifecycle(org_id,knowledge_base_id) VALUES(o,k)
          ON CONFLICT(knowledge_base_id) DO NOTHING;
        SELECT * INTO r FROM public.rag_kb_lifecycle WHERE org_id=o AND knowledge_base_id=k FOR UPDATE;
        IF NOT FOUND OR r.state<>'active' OR (g IS NOT NULL AND r.generation<>g) THEN
          RAISE EXCEPTION 'rag_frozen' USING ERRCODE='55000';
        END IF;
        PERFORM set_config('app.rag_generation',r.generation::text,true);
        RETURN r.generation;
      END $$""")
    op.execute("""CREATE FUNCTION rag_document_guard() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE d record; actor name := coalesce(nullif(current_setting('role',true),'none'),session_user); BEGIN
        IF TG_OP='DELETE' THEN d:=OLD; ELSE d:=NEW; END IF;
        IF TG_OP='UPDATE' AND (OLD.id,OLD.org_id,OLD.knowledge_base_id) IS DISTINCT FROM (NEW.id,NEW.org_id,NEW.knowledge_base_id) THEN
          RAISE EXCEPTION 'rag_scope_immutable' USING ERRCODE='55000';
        END IF;
        IF NOT pg_has_role(actor,'rag_lifecycle','member') AND NOT
          (SELECT rolsuper FROM pg_roles WHERE rolname=actor) THEN
          PERFORM public.rag_writer_fence(d.org_id,d.knowledge_base_id,coalesce(nullif(current_setting('app.rag_generation',true),'')::bigint,1));
        ELSIF TG_OP='INSERT' THEN
          PERFORM set_config('app.current_org_id',d.org_id::text,true);
          INSERT INTO public.rag_kb_lifecycle(org_id,knowledge_base_id) VALUES(d.org_id,d.knowledge_base_id) ON CONFLICT DO NOTHING;
        END IF;
        RETURN d;
      END $$""")
    op.execute('CREATE TRIGGER rag_document_guard BEFORE INSERT OR UPDATE OR DELETE ON documents FOR EACH ROW EXECUTE FUNCTION rag_document_guard()')
    for name, args in [('rag_writer_fence','uuid,uuid,bigint'),('rag_document_guard','')]:
        op.execute(f'ALTER FUNCTION {name}({args}) OWNER TO rag_schema_owner')
        op.execute(f'REVOKE ALL ON FUNCTION {name}({args}) FROM PUBLIC')
    op.execute('GRANT EXECUTE ON FUNCTION rag_writer_fence(uuid,uuid,bigint) TO rag_app,rag_lifecycle')

    op.execute("""CREATE FUNCTION rag_begin_external(p uuid,o uuid,k uuid,g bigint,d uuid,t text)
      RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        PERFORM public.rag_writer_fence(o,k,g);
        IF t NOT IN ('vector_upsert','vector_delete') OR NOT EXISTS(SELECT FROM public.documents WHERE id=d AND org_id=o AND knowledge_base_id=k) THEN
          RAISE EXCEPTION 'rag_scope_invalid' USING ERRCODE='55000';
        END IF;
        IF EXISTS(SELECT FROM public.rag_external_operations WHERE org_id=o AND knowledge_base_id=k AND document_id=d AND NOT settled) THEN
          RAISE EXCEPTION 'rag_external_uncertain' USING ERRCODE='55000';
        END IF;
        INSERT INTO public.rag_external_operations(operation_id,org_id,knowledge_base_id,generation,document_id,kind) VALUES(p,o,k,g,d,t);
      END $$""")
    op.execute("""CREATE FUNCTION rag_settle_external(p uuid,o uuid,k uuid,g bigint)
      RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        PERFORM public.rag_writer_fence(o,k,g);
        UPDATE public.rag_external_operations SET settled=true
          WHERE operation_id=p AND org_id=o AND knowledge_base_id=k AND generation=g;
        IF NOT FOUND THEN RAISE EXCEPTION 'rag_operation_unknown' USING ERRCODE='55000'; END IF;
      END $$""")
    for name,args in [('rag_begin_external','uuid,uuid,uuid,bigint,uuid,text'),('rag_settle_external','uuid,uuid,uuid,bigint')]:
        op.execute(f'ALTER FUNCTION {name}({args}) OWNER TO rag_schema_owner')
        op.execute(f'REVOKE ALL ON FUNCTION {name}({args}) FROM PUBLIC')
        op.execute(f'GRANT EXECUTE ON FUNCTION {name}({args}) TO rag_app,rag_lifecycle')

    op.execute("""CREATE FUNCTION rag_prepare_index(p uuid,o uuid,k uuid,g bigint,d uuid,w text,q text)
      RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        PERFORM public.rag_writer_fence(o,k,g);
        IF w <> 'rag-index-'||d::text||'-'||p::text OR NOT EXISTS(SELECT FROM public.documents WHERE id=d AND org_id=o AND knowledge_base_id=k) THEN
          RAISE EXCEPTION 'rag_index_identity_invalid' USING ERRCODE='55000';
        END IF;
        INSERT INTO public.rag_index_executions(operation_id,org_id,knowledge_base_id,generation,document_id,workflow_id,task_queue) VALUES(p,o,k,g,d,w,q);
      END $$""")
    op.execute("""CREATE FUNCTION rag_register_index(p uuid,o uuid,k uuid,g bigint,d uuid,w text,r uuid,q text)
      RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        PERFORM public.rag_writer_fence(o,k,g);
        UPDATE public.rag_index_executions SET run_id=r WHERE operation_id=p AND org_id=o AND knowledge_base_id=k AND generation=g AND document_id=d AND workflow_id=w AND task_queue=q AND (run_id IS NULL OR run_id=r);
        IF NOT FOUND THEN RAISE EXCEPTION 'rag_index_identity_invalid' USING ERRCODE='55000'; END IF;
      END $$""")
    for name,args in [('rag_prepare_index','uuid,uuid,uuid,bigint,uuid,text,text'),('rag_register_index','uuid,uuid,uuid,bigint,uuid,text,uuid,text')]:
        op.execute(f'ALTER FUNCTION {name}({args}) OWNER TO rag_schema_owner')
        op.execute(f'REVOKE ALL ON FUNCTION {name}({args}) FROM PUBLIC')
        op.execute(f'GRANT EXECUTE ON FUNCTION {name}({args}) TO rag_app,rag_lifecycle')

    op.execute('CREATE POLICY receipt_discovery ON rag_index_executions FOR SELECT TO rag_schema_owner USING (true)')
    op.execute("""CREATE FUNCTION rag_pending_index_executions(cursor_id uuid)
      RETURNS TABLE(operation_id uuid,org_id uuid,knowledge_base_id uuid,document_id uuid,generation bigint,workflow_id text,run_id uuid,task_queue text)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      SELECT operation_id,org_id,knowledge_base_id,document_id,generation,workflow_id,run_id,task_queue
      FROM public.rag_index_executions WHERE closed_at IS NULL AND (cursor_id IS NULL OR operation_id>cursor_id)
      ORDER BY operation_id LIMIT 100
      $$""")
    op.execute('ALTER FUNCTION rag_pending_index_executions(uuid) OWNER TO rag_schema_owner')
    op.execute('REVOKE ALL ON FUNCTION rag_pending_index_executions(uuid) FROM PUBLIC')
    op.execute('GRANT EXECUTE ON FUNCTION rag_pending_index_executions(uuid) TO rag_lifecycle')

    extend_namespace()

def extend_namespace() -> None:
    op.execute("ALTER TABLE rag_index_executions ADD COLUMN namespace text NOT NULL DEFAULT 'default'")
    op.execute('DROP FUNCTION rag_prepare_index(uuid,uuid,uuid,bigint,uuid,text,text)')
    op.execute('DROP FUNCTION rag_register_index(uuid,uuid,uuid,bigint,uuid,text,uuid,text)')
    op.execute("""CREATE FUNCTION rag_prepare_index(p uuid,o uuid,k uuid,g bigint,d uuid,w text,q text,n text DEFAULT 'default')
      RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        PERFORM public.rag_writer_fence(o,k,g);
        IF length(n) NOT BETWEEN 1 AND 200 OR w <> 'rag-index-'||d::text||'-'||p::text OR NOT EXISTS(SELECT FROM public.documents WHERE id=d AND org_id=o AND knowledge_base_id=k) THEN
          RAISE EXCEPTION 'rag_index_identity_invalid' USING ERRCODE='55000';
        END IF;
        INSERT INTO public.rag_index_executions(operation_id,org_id,knowledge_base_id,generation,document_id,workflow_id,task_queue,namespace) VALUES(p,o,k,g,d,w,q,n);
      END $$""")
    op.execute("""CREATE FUNCTION rag_register_index(p uuid,o uuid,k uuid,g bigint,d uuid,w text,r uuid,q text,n text DEFAULT 'default')
      RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      BEGIN
        PERFORM public.rag_writer_fence(o,k,g);
        UPDATE public.rag_index_executions SET run_id=r WHERE operation_id=p AND org_id=o AND knowledge_base_id=k AND generation=g AND document_id=d AND workflow_id=w AND task_queue=q AND namespace=n AND (run_id IS NULL OR run_id=r);
        IF NOT FOUND THEN RAISE EXCEPTION 'rag_index_identity_invalid' USING ERRCODE='55000'; END IF;
      END $$""")
    for name,args in [('rag_prepare_index','uuid,uuid,uuid,bigint,uuid,text,text,text'),('rag_register_index','uuid,uuid,uuid,bigint,uuid,text,uuid,text,text')]:
        op.execute(f'ALTER FUNCTION {name}({args}) OWNER TO rag_schema_owner')
        op.execute(f'REVOKE ALL ON FUNCTION {name}({args}) FROM PUBLIC')
        op.execute(f'GRANT EXECUTE ON FUNCTION {name}({args}) TO rag_app,rag_lifecycle')
    op.execute('DROP FUNCTION rag_pending_index_executions(uuid)')
    op.execute("""CREATE FUNCTION rag_pending_index_executions(cursor_id uuid)
      RETURNS TABLE(operation_id uuid,org_id uuid,knowledge_base_id uuid,document_id uuid,generation bigint,workflow_id text,run_id uuid,task_queue text,namespace text)
      LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      SELECT operation_id,org_id,knowledge_base_id,document_id,generation,workflow_id,run_id,task_queue,namespace
      FROM public.rag_index_executions WHERE closed_at IS NULL AND (cursor_id IS NULL OR operation_id>cursor_id)
      ORDER BY operation_id LIMIT 100
      $$""")
    op.execute('ALTER FUNCTION rag_pending_index_executions(uuid) OWNER TO rag_schema_owner')
    op.execute('REVOKE ALL ON FUNCTION rag_pending_index_executions(uuid) FROM PUBLIC')
    op.execute('GRANT EXECUTE ON FUNCTION rag_pending_index_executions(uuid) TO rag_lifecycle')


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM rag_kb_lifecycle WHERE state<>'active'")):
        raise RuntimeError('rag_restore_before_downgrade')
    op.execute('DROP FUNCTION IF EXISTS rag_pending_index_executions(uuid)')
    op.execute('DROP TRIGGER rag_document_guard ON documents')
    op.execute('DROP FUNCTION rag_document_guard()')
    op.execute('DROP FUNCTION IF EXISTS rag_prepare_index(uuid,uuid,uuid,bigint,uuid,text,text,text)')
    op.execute('DROP FUNCTION IF EXISTS rag_register_index(uuid,uuid,uuid,bigint,uuid,text,uuid,text,text)')
    op.execute('DROP FUNCTION IF EXISTS rag_begin_external(uuid,uuid,uuid,bigint,uuid,text)')
    op.execute('DROP FUNCTION IF EXISTS rag_settle_external(uuid,uuid,uuid,bigint)')
    op.execute('DROP FUNCTION rag_writer_fence(uuid,uuid,bigint)')
    for table in ('rag_archive_steps','rag_external_operations','rag_index_executions','rag_archive_jobs','rag_kb_lifecycle'):
        op.execute(f'DROP TABLE {table}')
