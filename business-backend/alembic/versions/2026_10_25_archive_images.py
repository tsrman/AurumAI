"""Archive probes recognize conversation images.

Revision ID: 20261025_archive_images
Revises: 20261025_usage_kind
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261025_archive_images"
down_revision: str | None = "20261025_usage_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "'conversations','messages','workflow_runs','workflow_node_runs','interaction_turns',"
    "'interaction_executions','tool_operations','user_questions','interaction_outbox',"
    "'web_sources','knowledge_bases','skills','file_metadata','audit_logs'"
)

_PREDICATES = """
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
          WHEN 'web_sources' THEN 'r.org_id=v.org_id AND r.operation_id=v.operation_id AND r.position=v.position'"""

_IMAGE_PREDICATE = """
          WHEN 'conversation_images' THEN 'r.storage_key=v.storage_key OR (r.org_id=v.org_id AND r.operation_id=v.operation_id)'"""


def _payload_exists(tables: str) -> str:
    return f"""CREATE OR REPLACE FUNCTION platform_archive_payload_exists(p_table text,p_id uuid) RETURNS boolean
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
      DECLARE present boolean;
      BEGIN
        IF p_table NOT IN ({tables}) THEN
          RAISE EXCEPTION 'Invalid archive table'; END IF;
        EXECUTE format('SELECT EXISTS(SELECT 1 FROM public.%I WHERE id=$1)',p_table) INTO present USING p_id;
        RETURN present;
      END $$"""


def _unique_collision(predicates: str) -> str:
    return f"""CREATE OR REPLACE FUNCTION platform_archive_unique_collision(p_table text,p_id uuid,p_values jsonb) RETURNS boolean
      LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public SET row_security=off AS $$
      DECLARE predicate text; present boolean;
      BEGIN
        predicate := CASE p_table{predicates}
          ELSE NULL END;
        IF predicate IS NULL OR p_id IS NULL OR jsonb_typeof(p_values)<>'object' THEN RAISE EXCEPTION 'Invalid archive unique probe'; END IF;
        EXECUTE format('SELECT EXISTS(SELECT 1 FROM public.%I r CROSS JOIN jsonb_populate_record(NULL::public.%I,$1) v WHERE r.id<>$2 AND (%s))',p_table,p_table,predicate)
          INTO present USING p_values,p_id;
        RETURN present;
      END $$"""


def _grants() -> None:
    # CREATE OR REPLACE keeps existing privileges; restate them exactly.
    for signature in (
        "platform_archive_payload_exists(text,uuid)",
        "platform_archive_unique_collision(text,uuid,jsonb)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC,aurum_ai_app")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO aurum_ai_lifecycle")


def upgrade() -> None:
    op.execute(_payload_exists(_TABLES + ",'conversation_images'"))
    op.execute(_unique_collision(_PREDICATES + _IMAGE_PREDICATE))
    _grants()


def downgrade() -> None:
    op.execute(_payload_exists(_TABLES))
    op.execute(_unique_collision(_PREDICATES))
    _grants()
