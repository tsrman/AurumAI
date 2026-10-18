"""Monthly payload partitions backed by permanent global identities.

Maintenance-window, transactional copy/switch. No online writers are supported.
"""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "20260913_partitions"
down_revision = "20260913_identity_sync"
branch_labels = None
depends_on = None

PARENTS = ("conversations", "messages", "audit_logs")
REGISTRIES = dict(
    conversations="conversation_identities",
    messages="message_identities",
    workflow_runs="workflow_run_identities",
    audit_logs="audit_log_identities",
)
FIELDS = dict(
    conversations="id,org_id,user_id,created_at",
    messages="id,org_id,conversation_id,turn_id,role,created_at",
    workflow_runs="id,org_id,conversation_id,turn_id,initiating_user_id,source_message_id,temporal_workflow_id,created_at",
    audit_logs="id,org_id,created_at",
)
OWNER = "aurum_ai_partition_owner"


def sql(statement):
    return op.get_bind().execute(sa.text(statement))


def secure(table):
    sql(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
    sql(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
    sql(
        f"CREATE POLICY tenant_isolation ON public.{table} USING (org_id = NULLIF(current_setting('app.current_org_id',true),'')::uuid) WITH CHECK (org_id = NULLIF(current_setting('app.current_org_id',true),'')::uuid)"
    )
    sql(f"GRANT SELECT,INSERT,UPDATE,DELETE ON public.{table} TO aurum_ai_app")


def incoming(target):
    return list(
        sql(f"""SELECT c.conrelid::regclass::text AS tab, c.conname,
        pg_get_constraintdef(c.oid) AS def FROM pg_constraint c
        WHERE c.contype='f' AND c.confrelid='public.{target}'::regclass
        AND c.conparentid=0 ORDER BY 1,2""").mappings()
    )


def retarget(target, replacement, only=None, skip_registries=False):
    for constraint in incoming(target):
        if skip_registries and constraint["tab"] in REGISTRIES.values():
            continue
        if only and constraint["tab"] != only:
            continue
        tab, name, definition = constraint["tab"], constraint["conname"], constraint["def"]
        sql(f'ALTER TABLE {tab} DROP CONSTRAINT "{name}"')
        definition = definition.replace(f"REFERENCES {target}(", f"REFERENCES {replacement}(")
        sql(f'ALTER TABLE {tab} ADD CONSTRAINT "{name}" {definition}')


def copy_table(source, target, partitioned):
    sql(
        f"CREATE TABLE {target} (LIKE {source} INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING STORAGE INCLUDING COMMENTS)"
        + (" PARTITION BY RANGE(created_at)" if partitioned else "")
    )
    sql(f"ALTER TABLE {target} ADD PRIMARY KEY (id" + (",created_at)" if partitioned else ")"))
    for row in sql(f"""SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public' AND tablename='{source}'
        AND indexname NOT IN (SELECT conname FROM pg_constraint WHERE conrelid='{source}'::regclass AND contype IN ('p','u'))""").mappings():
        definition = (
            row["indexdef"]
            .replace(row["indexname"], row["indexname"] + "_copy")
            .replace(f"ON public.{source} ", f"ON public.{target} ")
        )
        sql(definition)
    for row in sql(
        f"SELECT conname,pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE conrelid='{source}'::regclass AND contype='f' AND conparentid=0"
    ).mappings():
        if row["conname"].startswith("fk_payload_identity_"):
            continue
        sql(f'ALTER TABLE {target} ADD CONSTRAINT "{row["conname"]}" {row["def"]}')


def copy_rows(source, target):
    columns = ",".join(
        sql(
            f"SELECT attname FROM pg_attribute WHERE attrelid='{source}'::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum"
        ).scalars()
    )
    sql(f"INSERT INTO {target} ({columns}) SELECT {columns} FROM {source}")
    if sql(f"""SELECT EXISTS ((SELECT id,created_at,org_id FROM {source} EXCEPT SELECT id,created_at,org_id FROM {target})
        UNION ALL (SELECT id,created_at,org_id FROM {target} EXCEPT SELECT id,created_at,org_id FROM {source}))
        OR (SELECT count(*) FROM {source}) <> (SELECT count(*) FROM {target})""").scalar_one():
        raise RuntimeError("Partition copy identity/count mismatch")


def month(at):
    return at.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month(at):
    return (
        at.replace(year=at.year + 1, month=1) if at.month == 12 else at.replace(month=at.month + 1)
    )


def upgrade():
    if not sql(
        "SELECT EXISTS(SELECT 1 FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='pg_partman' AND n.nspname='partman' AND e.extversion='5.5.0')"
    ).scalar_one():
        raise RuntimeError("Install pinned pg_partman 5 in schema partman before this migration")
    sql("SET LOCAL timezone='UTC'")
    sql("SELECT pg_advisory_xact_lock(20260913, 2)")
    sql("LOCK TABLE conversations,messages,audit_logs,workflow_runs IN ACCESS EXCLUSIVE MODE")
    sql(
        f"DO $$ BEGIN CREATE ROLE {OWNER} NOLOGIN BYPASSRLS; EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )
    sql(f"GRANT USAGE,CREATE ON SCHEMA public TO {OWNER}")
    sql(f"GRANT USAGE ON SCHEMA partman TO {OWNER}")
    sql(f"GRANT ALL ON ALL TABLES IN SCHEMA partman TO {OWNER}")
    sql(f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA partman TO {OWNER}")
    sql(f"GRANT ALL ON ALL SEQUENCES IN SCHEMA partman TO {OWNER}")
    for parent, registry in REGISTRIES.items():
        fields = FIELDS[parent]
        sql(f"CREATE TABLE {registry} AS SELECT {fields} FROM {parent} WITH NO DATA")
        for field in fields.split(","):
            if field != "turn_id" or parent != "messages":
                sql(f"ALTER TABLE {registry} ALTER COLUMN {field} SET NOT NULL")
        sql(f"ALTER TABLE {registry} ALTER COLUMN created_at SET DEFAULT now()")
        sql(
            f"ALTER TABLE {registry} ADD PRIMARY KEY(id), ADD UNIQUE(org_id,id), ADD UNIQUE(org_id,id,created_at)"
        )
        sql(
            f"ALTER TABLE {registry} ADD COLUMN lifecycle varchar(16) NOT NULL DEFAULT 'active' CHECK(lifecycle IN ('active','archiving','archived')), ADD COLUMN generation bigint NOT NULL DEFAULT 1 CHECK(generation>0), ADD COLUMN lease_id uuid, ADD COLUMN lease_until timestamptz, ADD COLUMN archive_id uuid"
        )
        sql(f"INSERT INTO {registry} ({fields}) SELECT {fields} FROM {parent}")
        secure(registry)
        sql(f"REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON {registry} FROM aurum_ai_app")
        sql(f"GRANT INSERT({fields}) ON {registry} TO aurum_ai_app")
        sql(f"GRANT UPDATE(created_at) ON {registry} TO aurum_ai_app")
    sql(
        "ALTER TABLE conversation_identities ADD UNIQUE(org_id,id,user_id), ADD FOREIGN KEY(org_id,user_id) REFERENCES users(org_id,id) ON DELETE CASCADE"
    )
    sql(
        "ALTER TABLE message_identities ADD UNIQUE(conversation_id,turn_id,role), ADD FOREIGN KEY(org_id,conversation_id) REFERENCES conversation_identities(org_id,id) ON DELETE CASCADE"
    )
    sql(
        "ALTER TABLE workflow_run_identities ADD UNIQUE(conversation_id,turn_id), ADD UNIQUE(temporal_workflow_id), ADD FOREIGN KEY(org_id,conversation_id) REFERENCES conversation_identities(org_id,id) ON DELETE CASCADE, ADD FOREIGN KEY(org_id,source_message_id) REFERENCES message_identities(org_id,id) ON DELETE CASCADE, ADD FOREIGN KEY(org_id,initiating_user_id) REFERENCES users(org_id,id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED"
    )
    for parent in PARENTS:
        retarget(parent, REGISTRIES[parent])
    retarget("workflow_runs", "workflow_run_identities", only="llm_usage_events")
    for parent in PARENTS:
        target = parent + "_new"
        copy_table(parent, target, True)
        dates = set(month(at) for at in sql(f"SELECT DISTINCT created_at FROM {parent}").scalars())
        current = month(datetime.now(timezone.utc))
        for _ in range(4):
            dates.add(current)
            current = next_month(current)
        for at in sorted(dates):
            child = f"{parent}_p{at:%Y%m%d}"
            sql(
                f"CREATE TABLE {child} PARTITION OF {target} FOR VALUES FROM ('{at.isoformat()}') TO ('{next_month(at).isoformat()}')"
            )
            secure(child)
        sql(f"CREATE TABLE {parent}_default PARTITION OF {target} DEFAULT")
        secure(parent + "_default")
        sql(
            f"ALTER TABLE {target} ADD CONSTRAINT fk_payload_identity_{parent} FOREIGN KEY(org_id,id,created_at) REFERENCES {REGISTRIES[parent]}(org_id,id,created_at) ON DELETE CASCADE"
        )
        copy_rows(parent, target)
        sql(f"ALTER TABLE {parent} RENAME TO {parent}_old")
        sql(f"ALTER TABLE {target} RENAME TO {parent}")
        sql(f"DROP TABLE {parent}_old")
        secure(parent)
        for table in [parent, parent + "_default"] + [
            f"{parent}_p{at:%Y%m%d}" for at in sorted(dates)
        ]:
            sql(f"ALTER TABLE {table} OWNER TO {OWNER}")
        sql(
            f"SELECT partman.create_parent(p_parent_table := 'public.{parent}', p_control := 'created_at', p_interval := '1 month', p_type := 'range', p_premake := 3, p_automatic_maintenance := 'off', p_start_partition := '{month(datetime.now(timezone.utc)).isoformat()}')"
        )
        sql(
            f"UPDATE partman.part_config SET retention=NULL, infinite_time_partitions=true, automatic_maintenance='off' WHERE parent_table='public.{parent}'"
        )
    sql(
        "ALTER TABLE workflow_runs ADD CONSTRAINT fk_payload_identity_workflow_runs FOREIGN KEY(org_id,id,created_at) REFERENCES workflow_run_identities(org_id,id,created_at) ON DELETE CASCADE"
    )
    sql(f"GRANT SELECT,DELETE ON interaction_turns,workflow_runs,skills,knowledge_bases TO {OWNER}")
    sql(f"GRANT SELECT ON llm_usage_events,conversation_identities TO {OWNER}")
    install_guards()
    install_maintenance()
    sql("SELECT platform_maintain_partitions()")


def install_guards():
    # Match the original FK's live-parent requirement with a payload row lock.
    # NO KEY UPDATE also serializes later conversation aggregate/title writes.
    # Payload locks MUST precede registry locks: DELETE obtains its row
    # lock before any BEFORE trigger, so registry-first admission can deadlock.
    sql("""CREATE FUNCTION platform_lock_live_conversation(p_org uuid,p_id uuid) RETURNS void
    LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
    DECLARE at timestamptz;
    BEGIN
      -- BEFORE triggers run before WITH CHECK. Preserve the original RLS
      -- rejection for a foreign tenant without probing its payload existence.
      IF row_security_active('public.conversations'::regclass)
         AND p_org IS DISTINCT FROM NULLIF(current_setting('app.current_org_id',true),'')::uuid THEN
        RAISE EXCEPTION 'new row violates row-level security policy' USING ERRCODE='42501';
      END IF;
      SELECT created_at INTO at FROM public.conversation_identities
        WHERE id=p_id AND org_id=p_org AND lifecycle='active';
      PERFORM 1 FROM public.conversations WHERE id=p_id AND org_id=p_org AND created_at=at FOR NO KEY UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Live conversation unavailable' USING ERRCODE='23503'; END IF;
    END $$""")
    sql("""CREATE FUNCTION platform_lock_live_message(p_org uuid,p_id uuid,p_conversation uuid) RETURNS void
    LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
    DECLARE at timestamptz;
    BEGIN
      SELECT created_at INTO at FROM public.message_identities
        WHERE id=p_id AND org_id=p_org AND conversation_id=p_conversation AND lifecycle='active';
      PERFORM 1 FROM public.messages WHERE id=p_id AND org_id=p_org AND conversation_id=p_conversation AND created_at=at FOR KEY SHARE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Live source message unavailable' USING ERRCODE='23503'; END IF;
    END $$""")
    sql("""CREATE FUNCTION platform_live_conversation_reference() RETURNS trigger
    LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
    DECLARE conversation uuid;
    BEGIN
      IF TG_TABLE_NAME='interaction_turns' THEN conversation:=NEW.conversation_id;
      ELSE conversation:=NEW.owning_conversation_id; END IF;
      IF conversation IS NULL THEN RETURN NEW; END IF;
      IF TG_OP='UPDATE' THEN
        IF TG_TABLE_NAME='interaction_turns' THEN
          IF NEW.org_id IS NOT DISTINCT FROM OLD.org_id AND NEW.conversation_id IS NOT DISTINCT FROM OLD.conversation_id THEN RETURN NEW; END IF;
        ELSE
          IF NEW.org_id IS NOT DISTINCT FROM OLD.org_id AND NEW.owning_conversation_id IS NOT DISTINCT FROM OLD.owning_conversation_id THEN RETURN NEW; END IF;
        END IF;
      END IF;
      PERFORM public.platform_lock_live_conversation(NEW.org_id,conversation);
      PERFORM 1 FROM public.conversation_identities WHERE id=conversation AND org_id=NEW.org_id AND lifecycle='active' FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Conversation identity unavailable' USING ERRCODE='23503'; END IF;
      RETURN NEW;
    END $$""")
    for table in ("interaction_turns", "skills", "knowledge_bases"):
        sql(
            f"CREATE TRIGGER live_conversation_reference BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION platform_live_conversation_reference()"
        )
    for parent, registry in REGISTRIES.items():
        fields = FIELDS[parent]
        values = ",".join("NEW." + field for field in fields.split(","))
        match = " AND ".join(
            f"r.{field} IS NOT DISTINCT FROM NEW.{field}" for field in fields.split(",")
        )
        unchanged = " AND ".join(
            f"OLD.{field} IS NOT DISTINCT FROM NEW.{field}" for field in fields.split(",")
        )
        conversation_guard = ""
        if parent in ("messages", "workflow_runs"):
            conversation_guard = "IF TG_OP='INSERT' THEN PERFORM public.platform_lock_live_conversation(NEW.org_id,NEW.conversation_id);"
            if parent == "workflow_runs":
                conversation_guard += "PERFORM public.platform_lock_live_message(NEW.org_id,NEW.source_message_id,NEW.conversation_id);"
            conversation_guard += """END IF; PERFORM 1 FROM public.conversation_identities WHERE id=NEW.conversation_id AND org_id=NEW.org_id AND lifecycle='active' FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Conversation identity unavailable' USING ERRCODE='23503'; END IF;"""
        sql(f"""CREATE FUNCTION platform_identity_{parent}() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
        BEGIN
            IF TG_OP='UPDATE' AND NOT ({unchanged}) THEN RAISE EXCEPTION 'Immutable payload identity' USING ERRCODE='23514'; END IF;
            {conversation_guard}
            INSERT INTO public.{registry}({fields}) VALUES({values}) ON CONFLICT(id) DO NOTHING;
            PERFORM 1 FROM public.{registry} r WHERE {match} AND r.lifecycle='active' FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Payload identity unavailable' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END $$""")
        sql(
            f"CREATE TRIGGER identity_guard BEFORE INSERT OR UPDATE ON {parent} FOR EACH ROW EXECUTE FUNCTION platform_identity_{parent}()"
        )
        sql(f"""CREATE FUNCTION platform_immutable_{registry}() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
        BEGIN IF NOT ({unchanged}) THEN RAISE EXCEPTION 'Immutable global identity' USING ERRCODE='23514'; END IF; RETURN NEW; END $$""")
        sql(
            f"CREATE TRIGGER immutable_identity BEFORE UPDATE ON {registry} FOR EACH ROW EXECUTE FUNCTION platform_immutable_{registry}()"
        )
    # Payload deletion remains a physical graph deletion. Identity tombstones stay.
    sql("""CREATE FUNCTION platform_delete_payload() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public,pg_temp AS $$
    BEGIN
      IF TG_TABLE_NAME LIKE 'conversations%' THEN
        DELETE FROM public.interaction_turns WHERE conversation_id=OLD.id AND org_id=OLD.org_id;
        DELETE FROM public.workflow_runs WHERE conversation_id=OLD.id AND org_id=OLD.org_id;
        DELETE FROM public.skills WHERE owning_conversation_id=OLD.id AND org_id=OLD.org_id;
        DELETE FROM public.knowledge_bases WHERE owning_conversation_id=OLD.id AND org_id=OLD.org_id;
        DELETE FROM public.messages WHERE conversation_id=OLD.id AND org_id=OLD.org_id;
      ELSIF TG_TABLE_NAME LIKE 'messages%' THEN
        DELETE FROM public.workflow_runs WHERE source_message_id=OLD.id AND org_id=OLD.org_id;
      ELSIF TG_TABLE_NAME='workflow_runs' AND EXISTS(SELECT 1 FROM public.llm_usage_events WHERE workflow_run_id=OLD.id AND org_id=OLD.org_id)
        AND EXISTS(SELECT 1 FROM public.conversation_identities WHERE id=OLD.conversation_id AND lifecycle='active') THEN
        RAISE EXCEPTION 'Billing referenced run requires archive lifecycle' USING ERRCODE='23503';
      END IF;
      RETURN OLD;
    END $$""")
    for parent in ("conversations", "messages", "workflow_runs"):
        sql(
            f"CREATE TRIGGER delete_payload_graph BEFORE DELETE ON {parent} FOR EACH ROW EXECUTE FUNCTION platform_delete_payload()"
        )
    sql("""CREATE FUNCTION platform_default_bound() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
    DECLARE n bigint;
    BEGIN
      PERFORM pg_advisory_xact_lock(20260913,2);
      EXECUTE format('SELECT count(*) FROM public.%I',TG_TABLE_NAME) INTO n;
      IF n>=10000 THEN RAISE EXCEPTION 'Partition default capacity exceeded; run maintenance' USING ERRCODE='54000'; END IF;
      RETURN NEW;
    END $$""")
    sql(f"ALTER FUNCTION platform_default_bound() OWNER TO {OWNER}")
    sql("REVOKE ALL ON FUNCTION platform_default_bound() FROM PUBLIC,aurum_ai_app")
    for parent in PARENTS:
        sql(
            f"CREATE TRIGGER bounded_default BEFORE INSERT ON {parent}_default FOR EACH ROW EXECUTE FUNCTION platform_default_bound()"
        )


def install_maintenance():
    # No procedure or autonomous commits: relation DDL and RLS become visible together.
    sql("""CREATE FUNCTION platform_ensure_partition(p_parent text,p_month timestamptz) RETURNS bigint
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,partman,pg_temp SET timezone='UTC' AS $$
    DECLARE lower_bound timestamptz; upper_bound timestamptz; child text; default_child text;
      staged bigint; inserted bigint; cols text;
    BEGIN
      IF p_parent NOT IN ('conversations','messages','audit_logs') OR p_month IS NULL THEN
        RAISE EXCEPTION 'Invalid partition target'; END IF;
      PERFORM pg_advisory_xact_lock(20260913,2);
      lower_bound:=date_trunc('month',p_month); upper_bound:=lower_bound+interval '1 month';
      child:=p_parent||'_p'||to_char(lower_bound,'YYYYMMDD'); default_child:=p_parent||'_default';
      IF to_regclass('public.'||child) IS NOT NULL THEN RETURN 0; END IF;
      LOCK TABLE public.conversations,public.messages,public.audit_logs IN ACCESS EXCLUSIVE MODE NOWAIT;
      EXECUTE format('SELECT count(*) FROM public.%I',default_child) INTO staged;
      IF staged>10000 THEN RAISE EXCEPTION 'Default overflow exceeds bounded maintenance batch'; END IF;
      SELECT string_agg(quote_ident(attname),',' ORDER BY attnum) INTO cols FROM pg_attribute
        WHERE attrelid=('public.'||p_parent)::regclass AND attnum>0 AND NOT attisdropped;
      EXECUTE format('CREATE TEMP TABLE partition_drain ON COMMIT DROP AS SELECT %s FROM public.%I WHERE created_at >= $1 AND created_at < $2',cols,default_child) USING lower_bound,upper_bound;
      -- Detaching is transactional and avoids delete graph triggers while moving payloads.
      EXECUTE format('ALTER TABLE public.%I DETACH PARTITION public.%I',p_parent,default_child);
      EXECUTE format('ALTER TABLE public.%I DISABLE TRIGGER USER',default_child);
      EXECUTE format('DELETE FROM public.%I WHERE created_at >= $1 AND created_at < $2',default_child) USING lower_bound,upper_bound;
      GET DIAGNOSTICS staged=ROW_COUNT;
      EXECUTE format('ALTER TABLE public.%I ENABLE TRIGGER USER',default_child);
      EXECUTE format('ALTER TABLE public.%I ATTACH PARTITION public.%I DEFAULT',p_parent,default_child);
      PERFORM partman.create_partition_time('public.'||p_parent,ARRAY[lower_bound]);
      PERFORM public.platform_secure_partitions();
      -- Original immutable identities were checked on admission. Moving rows must also work
      -- for archived lifecycle rows and cannot run the graph delete or write-admission guard.
      EXECUTE format('ALTER TABLE public.%I DISABLE TRIGGER USER',child);
      EXECUTE format('INSERT INTO public.%I (%s) SELECT %s FROM partition_drain',child,cols,cols);
      GET DIAGNOSTICS inserted=ROW_COUNT;
      EXECUTE format('ALTER TABLE public.%I ENABLE TRIGGER USER',child);
      IF inserted<>staged THEN RAISE EXCEPTION 'Partition drain count mismatch'; END IF;
      DROP TABLE partition_drain;
      RETURN inserted;
    END $$""")
    sql("""CREATE FUNCTION platform_secure_partitions() RETURNS void LANGUAGE plpgsql
    SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
    DECLARE child record;
    BEGIN
      -- This helper alters every tree, including when invoked by ensure-month.
      -- Secure the whole relation lock set before the first blocking ALTER.
      LOCK TABLE public.conversations,public.messages,public.audit_logs IN ACCESS EXCLUSIVE MODE NOWAIT;
      FOR child IN SELECT c.relname,c.relowner,p.relowner AS parentowner FROM pg_inherits i
        JOIN pg_class p ON p.oid=i.inhparent JOIN pg_class c ON c.oid=i.inhrelid
        WHERE p.oid IN ('public.conversations'::regclass,'public.messages'::regclass,'public.audit_logs'::regclass)
      LOOP
        IF child.relowner<>child.parentowner THEN RAISE EXCEPTION 'Unexpected partition owner'; END IF;
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',child.relname);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY',child.relname);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON public.%I',child.relname);
        EXECUTE format('CREATE POLICY tenant_isolation ON public.%I USING (org_id=NULLIF(current_setting(''app.current_org_id'',true),'''')::uuid) WITH CHECK (org_id=NULLIF(current_setting(''app.current_org_id'',true),'''')::uuid)',child.relname);
        EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC',child.relname);
        EXECUTE format('GRANT SELECT,INSERT,UPDATE,DELETE ON public.%I TO aurum_ai_app',child.relname);
      END LOOP;
    END $$""")
    sql("""CREATE FUNCTION platform_maintain_partitions() RETURNS bigint LANGUAGE plpgsql
    SECURITY DEFINER SET search_path=pg_catalog,public,partman,pg_temp SET timezone='UTC' AS $$
    DECLARE parent text; target timestamptz; targets timestamptz[]; moved bigint:=0;
    BEGIN
      PERFORM pg_advisory_xact_lock(20260913,2);
      -- ensure-month secures children across all parents. Acquire every tree
      -- before any creation/drain/security DDL can block on a later parent.
      LOCK TABLE public.conversations,public.messages,public.audit_logs IN ACCESS EXCLUSIVE MODE NOWAIT;
      FOREACH parent IN ARRAY ARRAY['conversations','messages','audit_logs'] LOOP
        EXECUTE format('SELECT array_agg(DISTINCT date_trunc(''month'',created_at)) FROM public.%I',parent||'_default') INTO targets;
        FOREACH target IN ARRAY coalesce(targets,ARRAY[]::timestamptz[]) LOOP
          moved:=moved+public.platform_ensure_partition(parent,target);
        END LOOP;
        FOR target IN SELECT generate_series(date_trunc('month',now()),date_trunc('month',now())+interval '3 months',interval '1 month') LOOP
          moved:=moved+public.platform_ensure_partition(parent,target);
        END LOOP;
        UPDATE partman.part_config SET retention=NULL,premake=3,automatic_maintenance='off',infinite_time_partitions=true WHERE parent_table='public.'||parent;
      END LOOP;
      PERFORM public.platform_secure_partitions();
      RETURN moved;
    EXCEPTION WHEN lock_not_available THEN
      RETURN NULL;
    END $$""")
    for routine in (
        "platform_ensure_partition(text,timestamptz)",
        "platform_secure_partitions()",
        "platform_maintain_partitions()",
    ):
        sql(f"ALTER FUNCTION {routine} OWNER TO {OWNER}")
        sql(f"REVOKE ALL ON FUNCTION {routine} FROM PUBLIC,aurum_ai_app")
    sql("GRANT EXECUTE ON FUNCTION platform_maintain_partitions() TO aurum_ai_app")


def downgrade():
    sql("SET LOCAL timezone='UTC'")
    sql("SELECT pg_advisory_xact_lock(20260913,2)")
    sql("LOCK TABLE conversations,messages,audit_logs,workflow_runs IN ACCESS EXCLUSIVE MODE")
    # Normal physical deletion leaves active identity tombstones with no archive
    # to restore. Only archive state or a real old-schema FK requires payload.
    for parent, registry in REGISTRIES.items():
        missing = (
            f"LEFT JOIN {parent} p ON p.id=r.id AND p.created_at=r.created_at WHERE p.id IS NULL"
        )
        if sql(
            f"SELECT EXISTS(SELECT 1 FROM {registry} r {missing} AND (r.lifecycle <> 'active' OR r.archive_id IS NOT NULL))"
        ).scalar_one():
            raise RuntimeError("Restore missing archived payloads before partition downgrade")
        references = sql(f"""
            SELECT c.conrelid::regclass::text AS tab,
                   string_agg(format('s.%I=r.%I',source.attname,target.attname),' AND ') AS predicate
            FROM pg_constraint c
            CROSS JOIN LATERAL unnest(c.conkey,c.confkey) AS keys(source,target)
            JOIN pg_attribute source ON source.attrelid=c.conrelid AND source.attnum=keys.source
            JOIN pg_attribute target ON target.attrelid=c.confrelid AND target.attnum=keys.target
            WHERE c.confrelid='{registry}'::regclass AND c.contype='f' AND c.conparentid=0
            GROUP BY c.oid,c.conrelid
        """).mappings()
        for reference in references:
            if reference["tab"] in REGISTRIES.values():
                continue
            if sql(
                f"SELECT EXISTS(SELECT 1 FROM {reference['tab']} s JOIN {registry} r ON {reference['predicate']} {missing})"
            ).scalar_one():
                raise RuntimeError(
                    "Restore missing archived payloads or referenced runtime payloads before partition downgrade"
                )
    for table in ("interaction_turns", "skills", "knowledge_bases"):
        sql(f"DROP TRIGGER live_conversation_reference ON {table}")
    for parent in ("conversations", "messages", "workflow_runs"):
        sql(f"DROP TRIGGER delete_payload_graph ON {parent}")
    for parent in REGISTRIES:
        sql(f"DROP TRIGGER identity_guard ON {parent}")
    sql("ALTER TABLE workflow_runs DROP CONSTRAINT fk_payload_identity_workflow_runs")
    for parent in PARENTS:
        sql(f"DELETE FROM partman.part_config WHERE parent_table='public.{parent}'")
        target = parent + "_new"
        copy_table(parent, target, False)
        copy_rows(parent, target)
        if parent == "conversations":
            sql(
                f"ALTER TABLE {target} ADD CONSTRAINT uq_conversations_org_id_id UNIQUE(org_id,id), ADD CONSTRAINT uq_conversations_org_id_owner UNIQUE(org_id,id,user_id)"
            )
        if parent == "messages":
            sql(
                f"ALTER TABLE {target} ADD CONSTRAINT uq_messages_org_id_id UNIQUE(org_id,id), ADD CONSTRAINT uq_messages_conv_turn_role UNIQUE(conversation_id,turn_id,role)"
            )
        sql(f"ALTER TABLE {parent} RENAME TO {parent}_old")
        sql(f"ALTER TABLE {target} RENAME TO {parent}")
        # Partitions are owned by this parent and drop normally, with no CASCADE.
        sql(f"DROP TABLE {parent}_old")
        secure(parent)
    for parent in PARENTS:
        retarget(REGISTRIES[parent], parent, skip_registries=True)
    retarget("workflow_run_identities", "workflow_runs", only="llm_usage_events")
    for registry in reversed(tuple(REGISTRIES.values())):
        sql(f"DROP TABLE {registry}")
    for parent, registry in REGISTRIES.items():
        sql(f"DROP FUNCTION platform_identity_{parent}()")
        sql(f"DROP FUNCTION platform_immutable_{registry}()")
    for routine in (
        "platform_maintain_partitions()",
        "platform_ensure_partition(text,timestamptz)",
        "platform_secure_partitions()",
        "platform_delete_payload()",
        "platform_default_bound()",
        "platform_live_conversation_reference()",
        "platform_lock_live_conversation(uuid,uuid)",
        "platform_lock_live_message(uuid,uuid,uuid)",
    ):
        sql(f"DROP FUNCTION {routine}")
