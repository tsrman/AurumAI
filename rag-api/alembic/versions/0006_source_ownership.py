"""Enforce stable source ownership without normalizing legacy objects.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE OR REPLACE FUNCTION rag_document_guard() RETURNS trigger
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
      DECLARE d record; actor name := coalesce(nullif(current_setting('role',true),'none'),session_user); BEGIN
        IF TG_OP='DELETE' THEN d:=OLD; ELSE d:=NEW; END IF;
        IF TG_OP='UPDATE' AND (OLD.id,OLD.org_id,OLD.knowledge_base_id) IS DISTINCT FROM (NEW.id,NEW.org_id,NEW.knowledge_base_id) THEN
          RAISE EXCEPTION 'rag_scope_immutable' USING ERRCODE='55000';
        END IF;
        IF NOT pg_has_role(actor,'rag_lifecycle','member') AND NOT
          (SELECT rolsuper FROM pg_roles WHERE rolname=actor) THEN
          IF TG_OP='UPDATE' AND OLD.source_key IS DISTINCT FROM NEW.source_key THEN
            RAISE EXCEPTION 'rag_source_immutable' USING ERRCODE='55000';
          END IF;
          IF TG_OP='INSERT' AND NEW.source_key IS NOT NULL AND NEW.source_key !~
            ('^documents/'||NEW.org_id::text||'/'||NEW.knowledge_base_id::text||'/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(\\.[a-zA-Z0-9]{1,16})?$') THEN
            RAISE EXCEPTION 'rag_source_owner_invalid' USING ERRCODE='55000';
          END IF;
          PERFORM public.rag_writer_fence(d.org_id,d.knowledge_base_id,coalesce(nullif(current_setting('app.rag_generation',true),'')::bigint,1));
        ELSIF TG_OP='INSERT' THEN
          PERFORM set_config('app.current_org_id',d.org_id::text,true);
          INSERT INTO public.rag_kb_lifecycle(org_id,knowledge_base_id) VALUES(d.org_id,d.knowledge_base_id) ON CONFLICT DO NOTHING;
        END IF;
        RETURN d;
      END $$""")
    op.execute(
        "CREATE INDEX ix_documents_source_reference ON documents(source_key) WHERE source_key IS NOT NULL"
    )


def downgrade() -> None:
    # Keep the safety guard on downgrade; 0005 downgrade removes the function.
    op.execute("DROP INDEX ix_documents_source_reference")
