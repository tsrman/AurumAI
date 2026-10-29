"""Hermes sessions left open without a turn are closed by the backend: their
session workflow could not, its runner token having expired with the
sandbox's idle time."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text, update

from src.models.conversation import Conversation
from src.models.hermes_session import HermesSession
from src.services.hermes_sessions import close_idle_sessions


async def test_only_sessions_idle_without_a_turn_past_the_limit_are_closed(hermes_env) -> None:
    now = datetime.now(timezone.utc)
    async with hermes_env.sessions() as setup:
        await setup.execute(text("SELECT set_config('app.current_org_id', :org, false)"), {"org": str(hermes_env.org_id)})
        conversations = [
            Conversation(id=uuid.uuid4(), org_id=hermes_env.org_id, user_id=hermes_env.user_id, model="m") for _ in range(3)
        ]
        setup.add_all(conversations)
        await setup.flush()
        idle, recent, working = (
            HermesSession(
                id=uuid.uuid4(), org_id=hermes_env.org_id, user_id=hermes_env.user_id,
                conversation_id=conversation.id, expires_at=now + timedelta(hours=20),
            )
            for conversation in conversations
        )
        setup.add_all([idle, recent, working])
        await setup.flush()
        working.active_turn_id = hermes_env.turn_id
        working.active_execution_id = hermes_env.execution_id
        await setup.flush()
        await setup.execute(
            update(HermesSession).where(HermesSession.id.in_([idle.id, working.id])).values(updated_at=now - timedelta(hours=2))
        )
        await setup.commit()
        ids = {"idle": idle.id, "recent": recent.id, "working": working.id}

    closed = await close_idle_sessions(hermes_env.sessions)

    async with hermes_env.sessions() as check:
        await check.execute(text("SELECT set_config('app.current_org_id', :org, false)"), {"org": str(hermes_env.org_id)})
        rows = {row.id: row for row in (await check.scalars(select(HermesSession).where(HermesSession.id.in_(ids.values())))).all()}

    assert closed >= 1
    assert rows[ids["idle"]].state == "closed" and rows[ids["idle"]].token_version == 2
    assert rows[ids["recent"]].state == "open" and rows[ids["working"]].state == "open"
