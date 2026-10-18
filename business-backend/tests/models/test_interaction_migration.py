"""Exercise migrated PostgreSQL constraints/RLS, not source-string contracts."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError


TABLES = (
    "interaction_turns",
    "interaction_executions",
    "tool_operations",
    "user_questions",
    "interaction_outbox",
    "web_sources",
)


@pytest.fixture
async def graph(db):
    present = (
        (
            await db.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename=ANY(:names)"
                ),
                {"names": list(TABLES)},
            )
        )
        .scalars()
        .all()
    )
    assert set(present) == set(TABLES), "interaction migration has not created the durable tables"

    async def create(org=None, user=None):
        ids = {
            key: uuid4()
            for key in (
                "org",
                "user",
                "conv",
                "turn",
                "exec",
                "op",
                "web_op",
                "q",
                "source",
            )
        }
        if org is None:
            await db.execute(
                text("INSERT INTO organizations (id,name,slug) VALUES (:org,'Q',:slug)"),
                {**ids, "slug": f"q-{ids['org']}"},
            )
            await db.execute(
                text(
                    "INSERT INTO users (id,org_id,keycloak_id,email) VALUES (:user,:org,:kc,:email)"
                ),
                {**ids, "kc": uuid4(), "email": f"{ids['user']}@example.test"},
            )
        else:
            ids.update(org=org, user=user)
        await db.execute(
            text(
                "INSERT INTO conversations (id,org_id,user_id,model) VALUES (:conv,:org,:user,'test')"
            ),
            ids,
        )
        await db.execute(
            text(
                "INSERT INTO interaction_turns (id,org_id,conversation_id,owner_id,limits) "
                "VALUES (:turn,:org,:conv,:user,'{}')"
            ),
            ids,
        )
        await db.execute(
            text(
                "INSERT INTO interaction_executions "
                "(id,org_id,turn_id,workflow_id,run_id,task_queue,phase) "
                "VALUES (:exec,:org,:turn,:wid,:run,'queue','ordinary')"
            ),
            {**ids, "wid": f"conversation-{ids['conv']}", "run": uuid4()},
        )
        await db.execute(
            text(
                "INSERT INTO tool_operations "
                "(id,org_id,turn_id,execution_id,invocation_id,kind,arguments_hash) "
                "VALUES (:op,:org,:turn,:exec,:inv,'ask_user',:hash)"
            ),
            {**ids, "inv": uuid4(), "hash": "a" * 64},
        )
        await db.execute(
            text(
                "INSERT INTO user_questions (id,org_id,turn_id,execution_id,operation_id,text,expires_at) "
                "VALUES (:q,:org,:turn,:exec,:op,'City?',now()+interval '1 hour')"
            ),
            ids,
        )
        await db.execute(
            text(
                "INSERT INTO tool_operations "
                "(id,org_id,turn_id,execution_id,invocation_id,kind,arguments_hash) "
                "VALUES (:web_op,:org,:turn,:exec,:inv,'web_search',:hash)"
            ),
            {**ids, "inv": uuid4(), "hash": "b" * 64},
        )
        await db.execute(
            text(
                "INSERT INTO interaction_outbox "
                "(id,org_id,turn_id,execution_id,question_id,question_version,event_kind) "
                "VALUES (:id,:org,:turn,:exec,:q,1,'question_status')"
            ),
            {**ids, "id": uuid4()},
        )
        await db.execute(
            text(
                "INSERT INTO web_sources "
                "(id,org_id,turn_id,execution_id,operation_id,position,kind,final_url) "
                "VALUES (:source,:org,:turn,:exec,:web_op,0,'search','https://example.org/')"
            ),
            ids,
        )
        return ids

    return create


async def test_only_one_active_turn_per_conversation(db, graph):
    ids = await graph()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO interaction_turns (id,org_id,conversation_id,owner_id,limits) "
                    "VALUES (:id,:org,:conv,:user,'{}')"
                ),
                {**ids, "id": uuid4()},
            )
    await db.execute(text("UPDATE interaction_turns SET status='completed' WHERE id=:turn"), ids)
    await db.execute(
        text(
            "INSERT INTO interaction_turns (id,org_id,conversation_id,owner_id,limits) "
            "VALUES (:id,:org,:conv,:user,'{}')"
        ),
        {**ids, "id": uuid4()},
    )


async def test_pending_question_unique_per_execution_and_operation(db, graph):
    ids = await graph()
    other_op = uuid4()
    await db.execute(
        text(
            "INSERT INTO tool_operations (id,org_id,turn_id,execution_id,invocation_id,kind,arguments_hash) "
            "VALUES (:id,:org,:turn,:exec,:inv,'ask_user',:hash)"
        ),
        {**ids, "id": other_op, "inv": uuid4(), "hash": "b" * 64},
    )
    query = text(
        "INSERT INTO user_questions (id,org_id,turn_id,execution_id,operation_id,text,expires_at) "
        "VALUES (:id,:org,:turn,:exec,:operation,'Again?',now()+interval '1 hour')"
    )
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(query, {**ids, "id": uuid4(), "operation": other_op})
    await db.execute(text("UPDATE user_questions SET status='cancelled' WHERE id=:q"), ids)
    # Closing a question frees the execution slot, but not its operation identity.
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(query, {**ids, "id": uuid4(), "operation": ids["op"]})
    await db.execute(query, {**ids, "id": uuid4(), "operation": other_op})


async def test_duplicate_invocation_cannot_allocate_another_operation(db, graph):
    ids = await graph()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO tool_operations "
                    "(id,org_id,turn_id,execution_id,invocation_id,kind,arguments_hash) "
                    "SELECT :id,org_id,turn_id,execution_id,invocation_id,kind,arguments_hash "
                    "FROM tool_operations WHERE id=:op"
                ),
                {**ids, "id": uuid4()},
            )


async def test_same_child_workflow_id_can_have_distinct_run_ids(db, graph):
    ids = await graph()
    await db.execute(
        text(
            "INSERT INTO interaction_executions "
            "(id,org_id,turn_id,workflow_id,run_id,task_queue,phase) "
            "SELECT :id,org_id,turn_id,workflow_id,:run,task_queue,phase "
            "FROM interaction_executions WHERE id=:exec"
        ),
        {**ids, "id": uuid4(), "run": uuid4()},
    )
    assert (
        await db.scalar(
            text("SELECT count(*) FROM interaction_executions WHERE turn_id=:turn"), ids
        )
        == 2
    )


@pytest.mark.parametrize(
    "case,same_org",
    [
        ("turn", False),
        ("execution", False),
        ("operation", False),
        ("operation", True),
        ("question", False),
        ("question", True),
        ("outbox", False),
        ("outbox", True),
    ],
)
async def test_correlation_rejects_foreign_org_or_turn(db, graph, case, same_org):
    ids = await graph()
    foreign = await graph(ids["org"], ids["user"]) if same_org else await graph()
    # References exist, but belong to another turn/organization.
    updates = {
        "turn": ("interaction_turns", "owner_id", foreign["user"]),
        "execution": ("interaction_executions", "org_id", foreign["org"]),
        "operation": ("tool_operations", "execution_id", foreign["exec"]),
        "question": ("user_questions", "operation_id", foreign["op"]),
        "outbox": ("interaction_outbox", "question_id", foreign["q"]),
    }
    table, column, value = updates[case]
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                text(f"UPDATE {table} SET {column}=:foreign WHERE org_id=:org"),
                {**ids, "foreign": value},
            )


async def test_rls_runtime_role_hides_foreign_rows_and_rejects_writes(db, graph):
    own = await graph()
    foreign = await graph()
    await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
    await db.execute(
        text("SELECT set_config('app.current_org_id', :org, true)"), {"org": str(own["org"])}
    )
    assert await db.scalar(text("SELECT current_user")) == "aurum_ai_app"
    for table in TABLES:
        assert (await db.execute(text(f"SELECT DISTINCT org_id FROM {table}"))).scalars().all() == [
            own["org"]
        ]
        assert (
            await db.execute(
                text(f"UPDATE {table} SET updated_at=now() WHERE org_id=:org RETURNING id"),
                {"org": foreign["org"]},
            )
        ).all() == []
    with pytest.raises(ProgrammingError):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO interaction_turns (id,org_id,conversation_id,owner_id,limits) "
                    "VALUES (:id,:org,:conv,:user,'{}')"
                ),
                {**foreign, "id": uuid4()},
            )


async def test_question_cannot_be_answered_without_answer_identity_and_payload(db, graph):
    ids = await graph()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(text("UPDATE user_questions SET status='answered' WHERE id=:q"), ids)


@pytest.mark.parametrize(
    "assignment",
    [
        "consumed_at=now()",
        "text=E'\\t\\n'",
        "expires_at=created_at",
        "version=0",
    ],
)
async def test_question_rejects_invalid_lifecycle_or_empty_data(db, graph, assignment):
    ids = await graph()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(text(f"UPDATE user_questions SET {assignment} WHERE id=:q"), ids)


async def test_turn_cannot_substitute_another_owner_in_same_org(db, graph):
    ids = await graph()
    stranger = uuid4()
    await db.execute(
        text("INSERT INTO users (id,org_id,keycloak_id,email) VALUES (:stranger,:org,:kc,:email)"),
        {**ids, "stranger": stranger, "kc": uuid4(), "email": f"{stranger}@example.test"},
    )
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                text("UPDATE interaction_turns SET owner_id=:stranger WHERE id=:turn"),
                {**ids, "stranger": stranger},
            )


async def test_all_interaction_tables_force_rls_and_hide_rows_without_context(db, graph):
    await graph()
    flags = (
        await db.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname=ANY(:names)"
            ),
            {"names": list(TABLES)},
        )
    ).all()
    assert {name: (enabled, forced) for name, enabled, forced in flags} == {
        name: (True, True) for name in TABLES
    }
    await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
    await db.execute(text("SELECT set_config('app.current_org_id', '', true)"))
    for table in TABLES:
        assert await db.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_orm_roundtrip_uses_migrated_tables(db, graph):
    ids = await graph()
    from src.models.interaction import InteractionTurn, UserQuestion

    turn = await db.get(InteractionTurn, ids["turn"])
    question = await db.get(UserQuestion, ids["q"])
    assert turn is not None and turn.conversation_id == ids["conv"]
    assert question is not None and question.text == "City?"
    assert question.answer is None and question.status == "pending"


async def test_web_result_sweeper_is_granted_and_null_batch_is_bounded(db, graph):
    ids = await graph()
    await db.execute(
        text(
            "UPDATE tool_operations SET result='{}', result_expires_at=clock_timestamp() "
            "WHERE id=:web_op"
        ),
        ids,
    )
    await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
    await db.execute(text("SELECT set_config('app.current_org_id', '', true)"))

    assert await db.scalar(text("SELECT public.scrub_expired_web_results(NULL)")) == 0
    await db.execute(text("RESET ROLE"))
    assert await db.scalar(
        text("SELECT result IS NOT NULL FROM tool_operations WHERE id=:web_op"), ids
    )


async def test_web_source_kind_must_match_its_operation(db, graph):
    ids = await graph()
    with pytest.raises(IntegrityError, match="web source operation kind mismatch"):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO web_sources "
                    "(id,org_id,turn_id,execution_id,operation_id,position,kind,final_url) "
                    "VALUES (:id,:org,:turn,:exec,:op,1,'search','https://example.org/bad')"
                ),
                {**ids, "id": uuid4()},
            )


async def test_web_source_ids_cannot_collide_across_tenants(db, graph):
    first = await graph()
    second = await graph()
    with pytest.raises(IntegrityError, match="web_sources_pkey"):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO web_sources "
                    "(id,org_id,turn_id,execution_id,operation_id,position,kind,final_url) "
                    "VALUES (:id,:org,:turn,:exec,:web_op,1,'search','https://example.org/other')"
                ),
                {**second, "id": first["source"]},
            )


async def test_orm_inserts_complete_chain_with_database_defaults(db, graph):
    from datetime import UTC, datetime, timedelta
    from src.models.interaction import (
        InteractionExecution,
        InteractionOutbox,
        InteractionTurn,
        ToolOperation,
        UserQuestion,
    )
    from src.schemas.interaction import TurnLimits

    ids = await graph()
    await db.execute(text("UPDATE interaction_turns SET status='completed' WHERE id=:turn"), ids)
    turn = InteractionTurn(
        org_id=ids["org"],
        conversation_id=ids["conv"],
        owner_id=ids["user"],
        limits=TurnLimits().model_dump(),
    )
    db.add(turn)
    await db.flush()
    execution = InteractionExecution(
        org_id=ids["org"],
        turn_id=turn.id,
        workflow_id="ordinary-test",
        run_id=uuid4(),
        task_queue="test-queue",
        phase="ordinary",
    )
    db.add(execution)
    await db.flush()
    operation = ToolOperation(
        org_id=ids["org"],
        turn_id=turn.id,
        execution_id=execution.id,
        invocation_id=uuid4(),
        kind="ask_user",
        arguments_hash="c" * 64,
    )
    db.add(operation)
    await db.flush()
    question = UserQuestion(
        org_id=ids["org"],
        turn_id=turn.id,
        execution_id=execution.id,
        operation_id=operation.id,
        text="City?",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db.add(question)
    await db.flush()
    event = InteractionOutbox(
        org_id=ids["org"],
        turn_id=turn.id,
        execution_id=execution.id,
        question_id=question.id,
        question_version=1,
        event_kind="question_status",
    )
    db.add(event)
    await db.flush()
    assert turn.status == "active" and turn.question_count == 0
    assert execution.iteration == 1 and operation.attempts == 0
    assert question.options == [] and question.answer is None
    assert event.status == "pending" and event.available_at is not None


async def test_protected_wait_function_reads_active_wait_through_forced_rls(db, graph):
    ids = await graph()
    await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
    await db.execute(text("SELECT set_config('app.current_org_id', '', true)"))

    rows = (
        await db.execute(
            text("SELECT * FROM public.protected_interaction_waits(:conv, 300)"),
            {"conv": ids["conv"]},
        )
    ).mappings().all()

    assert [dict(row) for row in rows] == [
        {
            "org_id": ids["org"],
            "conversation_id": ids["conv"],
            "owner_id": ids["user"],
            "question_status": "pending",
            "expires_at": rows[0]["expires_at"],
            "settlement_until": rows[0]["expires_at"] + timedelta(minutes=5),
            "reconciliation": False,
        }
    ]


async def test_protected_wait_function_bounds_answered_and_expired_settlement(db, graph):
    answered = await graph()
    expired = await graph()
    overdue_pending = await graph()
    consumed = await graph()
    terminal = await graph()
    await db.execute(
        text(
            "UPDATE user_questions SET status='answered', created_at=clock_timestamp()-interval '2 hours', "
            "expires_at=clock_timestamp()-interval '1 second', "
            "answer=CAST(:answer AS jsonb), answered_at=clock_timestamp()-interval '2 seconds', "
            "answered_by=:user, answer_key=:key WHERE id=:q"
        ),
        {**answered, "key": uuid4(), "answer": '{"text":"ok"}'},
    )
    await db.execute(
        text(
            "UPDATE user_questions SET status='expired', created_at=clock_timestamp()-interval '2 hours', "
            "expires_at=clock_timestamp()-interval '1 second' WHERE id=:q"
        ),
        expired,
    )
    await db.execute(
        text(
            "UPDATE user_questions SET created_at=clock_timestamp()-interval '2 hours', "
            "expires_at=clock_timestamp()-interval '1 second' WHERE id=:q"
        ),
        overdue_pending,
    )
    await db.execute(
        text(
            "UPDATE user_questions SET status='answered', created_at=clock_timestamp()-interval '2 hours', "
            "expires_at=clock_timestamp()-interval '1 second', "
            "answer=CAST(:answer AS jsonb), answered_at=clock_timestamp()-interval '2 seconds', "
            "answered_by=:user, answer_key=:key, consumed_at=clock_timestamp() WHERE id=:q"
        ),
        {**consumed, "key": uuid4(), "answer": '{"text":"done"}'},
    )
    await db.execute(
        text("UPDATE interaction_turns SET status='cancelled' WHERE id=:turn"), terminal
    )

    rows = (
        await db.execute(
            text("SELECT * FROM public.protected_interaction_waits(NULL, 300)")
        )
    ).mappings().all()
    by_conversation = {row["conversation_id"]: row for row in rows}

    assert by_conversation[answered["conv"]]["reconciliation"] is False
    assert by_conversation[expired["conv"]]["reconciliation"] is True
    assert by_conversation[overdue_pending["conv"]]["reconciliation"] is True
    assert consumed["conv"] not in by_conversation
    assert terminal["conv"] not in by_conversation
