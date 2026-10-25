"""An open, unexpired Hermes session keeps its conversation out of the archive."""

from datetime import datetime, timedelta, timezone

from uuid import uuid4

from sqlalchemy import select, text

from src.models.conversation import Conversation
from src.models.data_identity import ConversationIdentity
from src.services.archive.service import ArchiveService
from src.services.conversation_lifecycle import eligibility_blocker
from tests.services.test_business_archive_images import _history
from tests.services.test_business_archive_service import (  # noqa: F401
    LocalArchiveStorage,
    archive_fixture,
)


async def _blocker(env) -> str | None:  # noqa: ANN001
    async with env.sessions() as session:
        identity = await session.scalar(
            select(ConversationIdentity).where(ConversationIdentity.id == env.conversation_id)
        )
        payload = await session.scalar(select(Conversation).where(Conversation.id == env.conversation_id))
        assert identity is not None and payload is not None
        return await eligibility_blocker(
            session, identity, payload, datetime.now(timezone.utc) + timedelta(days=1)
        )


async def test_open_session_blocks_archiving_until_closed_or_expired(hermes_env) -> None:
    await hermes_env.finish_turn()
    body = await hermes_env.open_session(bind=False)

    while_open = await _blocker(hermes_env)
    await hermes_env.expire_session(body["sessionId"])
    after_expiry = await _blocker(hermes_env)
    reopened = await hermes_env.open_session(bind=False)
    await hermes_env.internal("POST", f"/sessions/{reopened['sessionId']}/close")
    after_close = await _blocker(hermes_env)

    assert while_open == "hermes_session_open"
    assert reopened["sessionId"] != body["sessionId"]
    assert after_expiry != "hermes_session_open"
    assert after_close != "hermes_session_open"


async def _add_session(sessions, args: dict, *, state: str, expired: bool, turn: bool) -> None:  # noqa: ANN001
    async with sessions() as db:
        await db.execute(
            text(
                "INSERT INTO hermes_sessions(id,org_id,user_id,conversation_id,state,active_turn_id,expires_at,closed_at) "
                "VALUES(:id,:o,:u,:c,:state,:turn,now() + make_interval(hours => :h),"
                " CASE WHEN :state='closed' THEN now() END)"
            ),
            dict(args, id=uuid4(), state=state, turn=args["t"] if turn else None, h=-1 if expired else 1),
        )
        await db.commit()


async def _session_count(sessions, args: dict) -> int:  # noqa: ANN001
    async with sessions() as db:
        return int(
            await db.scalar(text("SELECT count(*) FROM hermes_sessions WHERE conversation_id=:c"), args)
        )


async def test_open_session_blocks_the_archive_run(archive_fixture, tmp_path) -> None:  # noqa: F811
    sessions, org, conversation = archive_fixture
    args = await _history(sessions, org, conversation)
    await _add_session(sessions, args, state="open", expired=False, turn=False)
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))

    result = await service.run(org, conversation, apply=True)

    assert result == {"eligible": False, "blocked_reason": "hermes_session_open"}
    assert await _session_count(sessions, args) == 1


async def test_purge_removes_closed_and_expired_sessions_and_restore_leaves_none(
    archive_fixture, tmp_path  # noqa: F811
) -> None:
    sessions, org, conversation = archive_fixture
    args = await _history(sessions, org, conversation)
    await _add_session(sessions, args, state="closed", expired=False, turn=False)
    # Expired but never closed, still naming the turn the purge deletes.
    await _add_session(sessions, args, state="open", expired=True, turn=True)
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))

    result = await service.run(org, conversation, apply=True)

    assert result["state"] == "purged"
    assert await _session_count(sessions, args) == 0
    await service.restore(org, result["archive_id"], apply=True)
    assert await _session_count(sessions, args) == 0


async def test_lifecycle_role_sees_and_purges_sessions_only_in_its_org(
    archive_fixture  # noqa: F811
) -> None:
    from sqlalchemy import delete, func

    from src.models.hermes_session import HermesSession

    sessions, org, conversation = archive_fixture
    args = await _history(sessions, org, conversation)
    await _add_session(sessions, args, state="open", expired=False, turn=False)
    await _add_session(sessions, args, state="closed", expired=False, turn=False)
    async with sessions() as db:
        await db.execute(text("SET LOCAL ROLE aurum_ai_lifecycle"))
        await db.execute(text("SELECT set_config('app.current_org_id', :o, true)"), dict(o=str(uuid4())))
        foreign = await db.scalar(select(func.count()).select_from(HermesSession))
        await db.execute(text("SELECT set_config('app.current_org_id', :o, true)"), dict(o=str(org)))
        identity = await db.scalar(select(ConversationIdentity).where(ConversationIdentity.id == conversation))
        payload = await db.scalar(select(Conversation).where(Conversation.id == conversation))
        assert identity is not None and payload is not None
        blocker = await eligibility_blocker(db, identity, payload, datetime.now(timezone.utc))
        purged = await db.execute(
            delete(HermesSession).where(
                HermesSession.org_id == org, HermesSession.conversation_id == conversation
            )
        )
        await db.rollback()

    assert foreign == 0
    assert blocker == "hermes_session_open"
    assert purged.rowcount == 2  # type: ignore[attr-defined]
