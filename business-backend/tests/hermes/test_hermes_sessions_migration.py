"""hermes_sessions is org-isolated, granted like other conversation tables and
allows one open session per conversation."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.models.conversation import Conversation
from src.models.hermes_session import HermesSession


async def test_table_forces_row_security_with_an_org_policy(db) -> None:
    row = (
        await db.execute(
            text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'hermes_sessions'")
        )
    ).one()
    policies = (
        await db.execute(text("SELECT policyname FROM pg_policies WHERE tablename = 'hermes_sessions'"))
    ).scalars().all()
    grants = set(
        (
            await db.execute(
                text(
                    "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_name = 'hermes_sessions' AND grantee IN ('aurum_ai_app','aurum_ai_lifecycle')"
                )
            )
        ).all()
    )

    assert row == (True, True)
    assert policies == ["hermes_sessions_org_isolation"]
    assert grants == {
        (role, privilege)
        for role in ("aurum_ai_app", "aurum_ai_lifecycle")
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
    }


async def _conversation(db, test_org, test_user) -> Conversation:  # noqa: ANN001
    conversation = Conversation(org_id=test_org.id, user_id=test_user.id, model="m")
    db.add(conversation)
    await db.flush()
    return conversation


def _session(test_org, test_user, conversation, **values) -> HermesSession:  # noqa: ANN001, ANN003
    return HermesSession(
        id=uuid.uuid4(),
        org_id=test_org.id,
        user_id=test_user.id,
        conversation_id=conversation.id,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        **values,
    )


async def test_one_open_session_per_conversation(db, test_org, test_user) -> None:
    conversation = await _conversation(db, test_org, test_user)
    db.add(_session(test_org, test_user, conversation, state="closed", closed_at=datetime.now(timezone.utc)))
    db.add(_session(test_org, test_user, conversation))
    await db.flush()
    db.add(_session(test_org, test_user, conversation))

    with pytest.raises(IntegrityError):
        await db.flush()


@pytest.mark.parametrize(
    "values",
    [
        {"state": "closed"},
        {"workflow_phase": "node", "workflow_node_key": "a"},
        {"token_version": 0},
    ],
)
async def test_inconsistent_rows_are_refused(db, test_org, test_user, values) -> None:
    conversation = await _conversation(db, test_org, test_user)
    db.add(_session(test_org, test_user, conversation, **values))

    with pytest.raises(IntegrityError):
        await db.flush()


async def test_a_binding_names_an_execution_of_the_bound_turn(db, test_org, test_user) -> None:
    from src.models.interaction import InteractionExecution, InteractionTurn
    from src.schemas.interaction import TurnLimits

    conversation = await _conversation(db, test_org, test_user)
    turns = [
        InteractionTurn(
            org_id=test_org.id,
            conversation_id=conversation.id,
            owner_id=test_user.id,
            limits=TurnLimits().model_dump(),
            status=status,
        )
        for status in ("completed", "active")
    ]
    db.add_all(turns)
    await db.flush()
    other = InteractionExecution(
        org_id=test_org.id,
        turn_id=turns[0].id,
        workflow_id="other",
        run_id=uuid.uuid4(),
        task_queue="tools",
        phase="ordinary",
    )
    db.add(other)
    await db.flush()

    for values in ({"active_turn_id": turns[1].id}, {"active_turn_id": turns[1].id, "active_execution_id": other.id}):
        async with db.begin_nested():
            db.add(_session(test_org, test_user, conversation, **values))
            with pytest.raises(IntegrityError):
                await db.flush()
