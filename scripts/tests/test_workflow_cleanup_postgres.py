"""Opt-in C5 cleanup SQL against real model FK constraints, in an isolated DB.

WORKFLOW_CLEANUP_TEST_DATABASE must name an operator-created disposable database
starting interactive_cleanup_verify_. Each case owns a fresh schema, not live rows.
"""

import importlib
import importlib.util
from datetime import date
import os
from pathlib import Path
import pkgutil
import re
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

DATABASE = os.getenv("WORKFLOW_CLEANUP_TEST_DATABASE", "")
pytestmark = pytest.mark.skipif(
    not DATABASE, reason="requires isolated PostgreSQL cleanup database"
)


@pytest_asyncio.fixture
async def case(monkeypatch):
    assert re.fullmatch(r"interactive_cleanup_verify_[a-z0-9_]+", DATABASE)
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "business-backend"))
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    import src.models
    from src.core.database import Base

    for module in pkgutil.iter_modules(src.models.__path__):
        importlib.import_module(f"src.models.{module.name}")
    from src.models.organization import Organization
    from src.models.user import User
    from src.models.agent_definition import AgentDefinition
    from src.models.workflow_definition import WorkflowDefinition
    from src.models.conversation import Conversation
    from src.models.message import Message
    from src.models.workflow_run import WorkflowRun
    from src.models.interaction import InteractionTurn, InteractionExecution, ToolOperation
    from src.models.web_source import WebSource
    from src.models.llm_usage import LLMUsageEvent
    from src.models.authorization_audit_event import AuthorizationAuditEvent

    schema = "cleanup_" + uuid4().hex
    engine = create_async_engine(
        f"postgresql+asyncpg://postgres:postgres@localhost:5432/{DATABASE}",
        connect_args={"server_settings": {"search_path": schema}},
    )
    ids = {
        key: uuid4()
        for key in (
            "org",
            "user",
            "agent",
            "definition",
            "conversation",
            "message",
            "run",
            "turn",
            "execution",
            "operation",
            "source",
        )
    }
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine) as session:
            session.add(Organization(id=ids["org"], name="cleanup test", slug=schema))
            await session.flush()
            session.add(
                User(
                    id=ids["user"],
                    org_id=ids["org"],
                    keycloak_id=uuid4(),
                    email=f"{schema}@example.com",
                )
            )
            await session.flush()
            session.add(
                AgentDefinition(
                    id=ids["agent"],
                    org_id=ids["org"],
                    created_by=ids["user"],
                    name="agent",
                    model_id="fixture",
                    system_prompt="fixture",
                )
            )
            await session.flush()
            session.add(
                WorkflowDefinition(
                    id=ids["definition"],
                    org_id=ids["org"],
                    created_by=ids["user"],
                    name="workflow",
                    supervisor_agent_definition_id=ids["agent"],
                )
            )
            session.add(
                Conversation(
                    id=ids["conversation"],
                    org_id=ids["org"],
                    user_id=ids["user"],
                    title="owned",
                    model="fixture",
                )
            )
            await session.flush()
            session.add(
                Message(
                    id=ids["message"],
                    org_id=ids["org"],
                    conversation_id=ids["conversation"],
                    user_id=ids["user"],
                    turn_id=ids["turn"],
                    role="user",
                    content="fixture",
                )
            )
            await session.flush()
            session.add(
                WorkflowRun(
                    id=ids["run"],
                    org_id=ids["org"],
                    workflow_definition_id=ids["definition"],
                    workflow_definition_version=1,
                    initiating_user_id=ids["user"],
                    conversation_id=ids["conversation"],
                    turn_id=ids["turn"],
                    source_message_id=ids["message"],
                    temporal_workflow_id=schema,
                    execution_snapshot={},
                    status="completed",
                )
            )
            await session.flush()
            session.add(
                InteractionTurn(
                    id=ids["turn"],
                    org_id=ids["org"],
                    conversation_id=ids["conversation"],
                    owner_id=ids["user"],
                    workflow_run_id=ids["run"],
                    status="completed",
                    limits={},
                )
            )
            await session.flush()
            session.add(
                InteractionExecution(
                    id=ids["execution"],
                    org_id=ids["org"],
                    turn_id=ids["turn"],
                    workflow_id=schema,
                    run_id=uuid4(),
                    task_queue=schema,
                    phase="node",
                    node_key="alpha",
                    agent_definition_id=ids["agent"],
                )
            )
            await session.flush()
            session.add(
                ToolOperation(
                    id=ids["operation"],
                    org_id=ids["org"],
                    turn_id=ids["turn"],
                    execution_id=ids["execution"],
                    invocation_id=uuid4(),
                    kind="web_read",
                    arguments_hash="a" * 64,
                    status="completed",
                )
            )
            await session.flush()
            session.add(
                WebSource(
                    id=ids["source"],
                    org_id=ids["org"],
                    turn_id=ids["turn"],
                    execution_id=ids["execution"],
                    operation_id=ids["operation"],
                    position=0,
                    kind="read",
                    final_url="https://example.com",
                )
            )
            session.add(
                LLMUsageEvent(
                    org_id=ids["org"],
                    user_id=ids["user"],
                    request_id=uuid4(),
                    conversation_id=ids["conversation"],
                    workflow_run_id=ids["run"],
                    workflow_phase="node",
                    workflow_node_key="alpha",
                    period_start=date(2026, 9, 1),
                    model="fixture",
                    total_tokens=7,
                )
            )
            session.add(
                AuthorizationAuditEvent(
                    org_id=ids["org"],
                    actor_id=ids["user"],
                    action="grant",
                    resource_type="workflow",
                    resource_id=str(ids["definition"]),
                    before_state=[],
                    after_state=[],
                    correlation_id=schema,
                )
            )
            await session.commit()
        owned = {
            "agent": [],
            "template": [],
            "workflow": [
                {"id": str(ids["definition"]), "created_by": str(ids["user"]), "name": "workflow"}
            ],
            "conversation": [
                {"id": str(ids["conversation"]), "user_id": str(ids["user"]), "title": "owned"}
            ],
            "run": [{"id": str(ids["run"]), "initiating_user_id": str(ids["user"])}],
        }
        yield SimpleNamespace(engine=engine, schema=schema, ids=ids, owned=owned)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


def cleanup(case, *, legacy_order=False):
    path = Path(__file__).parents[1] / "workflow_templates_ui_e2e.py"
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location("cleanup_pg_contract", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    database = module.Database(module.Config())
    captured = []
    database.query = lambda sql, **parameters: captured.append((sql, parameters))
    database.delete_owned(SimpleNamespace(organization_id=str(case.ids["org"])), case.owned)
    sql, parameters = captured[0]
    if legacy_order:
        sql = re.sub(r"DELETE FROM interaction_turns.*?;\n", "", sql, count=1, flags=re.S)
    command = [
        "docker",
        "exec",
        "-i",
        "aurum-ai-postgres",
        "psql",
        "-X",
        "-q",
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        "-d",
        DATABASE,
    ]
    for key, value in parameters.items():
        command.extend(["-v", f"{key}={value}"])
    result = subprocess.run(
        command,
        input=f'SET search_path TO "{case.schema}";\n' + sql,
        text=True,
        capture_output=True,
        timeout=30,
    )
    # Only return success and an allowlisted FK diagnostic, never raw SQL/data.
    return result.returncode == 0, "fk_interaction_turn_workflow_run" in result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "negative", [None, "renamed", "owner", "unrecorded-run", "definition", "kb"]
)
async def test_cleanup_obeys_real_interaction_dependencies_and_rolls_back(case, negative):
    from sqlalchemy import text

    if negative == "renamed":
        case.owned["conversation"][0]["title"] = "stale-title"
    elif negative == "owner":
        case.owned["conversation"][0]["user_id"] = str(uuid4())
    elif negative == "unrecorded-run":
        case.owned["run"] = []
    elif negative == "definition":
        case.owned["workflow"] = []
    elif negative == "kb":
        from sqlalchemy.ext.asyncio import AsyncSession
        from src.models.knowledge_base import KnowledgeBase

        async with AsyncSession(case.engine) as session:
            session.add(
                KnowledgeBase(
                    org_id=case.ids["org"],
                    created_by=case.ids["user"],
                    name="owned attachment",
                    owning_conversation_id=case.ids["conversation"],
                )
            )
            await session.commit()
    succeeded, interaction_fk_failed = cleanup(case)
    assert succeeded is (
        negative is None
    ), f"cleanup outcome mismatch; interaction FK blocked={interaction_fk_failed}"
    async with case.engine.connect() as connection:
        for table in (
            "interaction_turns",
            "interaction_executions",
            "tool_operations",
            "web_sources",
            "workflow_runs",
            "conversations",
            "workflow_definitions",
        ):
            count = await connection.scalar(text(f'SELECT count(*) FROM "{table}"'))
            assert count == (0 if negative is None else 1), f"unexpected partial cleanup in {table}"
        usage = (
            await connection.execute(
                text("SELECT workflow_run_id,total_tokens FROM llm_usage_events")
            )
        ).one()
        assert usage.total_tokens == 7
        assert usage.workflow_run_id == (None if negative is None else case.ids["run"])
        assert await connection.scalar(text("SELECT count(*) FROM authorization_audit_events")) == 1


@pytest.mark.asyncio
async def test_legacy_cleanup_order_reproduces_real_restrict_fk(case):
    assert cleanup(case, legacy_order=True) == (False, True)
