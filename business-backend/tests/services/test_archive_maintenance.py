"""Definer maintenance functions must preserve frozen snapshot contents."""

# ruff: noqa: F811
import pytest
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from src.models.data_identity import ConversationIdentity
from src.models.interaction import InteractionOutbox, InteractionTurn
from tests.services.test_interaction_outbox import outbox_fixture  # noqa: F401


@pytest.mark.asyncio
async def test_obsolete_status_notification_is_cancelled_not_delivered(outbox_fixture):
    fixture = outbox_fixture
    async with fixture.sessions.begin() as session:
        await session.execute(
            update(InteractionTurn)
            .where(InteractionTurn.id == fixture.turn_id)
            .values(status="completed")
        )
    async with fixture.sessions.begin() as session:
        assert await session.scalar(text("SELECT cancel_terminal_question_status(100)")) >= 1
        statuses = list(
            (
                await session.scalars(
                    select(InteractionOutbox).where(
                        InteractionOutbox.turn_id == fixture.turn_id,
                        InteractionOutbox.event_kind == "question_status",
                    )
                )
            ).all()
        )
        assert statuses
        assert all(
            row.status == "cancelled"
            and row.delivered_at is None
            and row.error_code == "terminal_turn_obsolete"
            for row in statuses
        )


@pytest.mark.asyncio
async def test_frozen_outbox_is_not_claimed_or_rewritten_by_definer(outbox_fixture):
    fixture = outbox_fixture
    async with fixture.sessions.begin() as session:
        await session.execute(
            update(ConversationIdentity)
            .where(ConversationIdentity.id == fixture.conversation_id)
            .values(lifecycle="archiving")
        )
    async with fixture.sessions.begin() as session:
        assert (
            list(
                (
                    await session.execute(
                        text("SELECT * FROM claim_interaction_outbox(100,30,300)")
                    )
                ).all()
            )
            == []
        )
        row = await session.get(InteractionOutbox, fixture.outbox_id)
        assert row.status == "pending" and row.lease_id is None and row.attempts == 0


@pytest.mark.asyncio
async def test_direct_child_writer_releases_claim_snapshot_scrub_without_deadlock(outbox_fixture):
    from src.services.conversation_lifecycle import ArchiveBlocked, claim_conversation

    fixture = outbox_fixture
    async with fixture.sessions.begin() as session:
        await session.execute(
            update(InteractionTurn)
            .where(InteractionTurn.id == fixture.turn_id)
            .values(status="completed")
        )
        status_id = await session.scalar(
            select(InteractionOutbox.id).where(
                InteractionOutbox.turn_id == fixture.turn_id,
                InteractionOutbox.event_kind == "question_status",
            )
        )
    async with fixture.sessions() as writer, fixture.sessions() as claimant:
        await writer.execute(text("SET LOCAL ROLE aurum_ai_app"))
        await writer.execute(
            text("SELECT set_config('app.current_org_id',:org,true)"), {"org": str(fixture.org_id)}
        )
        await writer.execute(
            text("SELECT 1 FROM interaction_outbox WHERE id=:id FOR UPDATE"), {"id": status_id}
        )
        claimant_pid = await claimant.scalar(text("SELECT pg_backend_pid()"))

        async def claim():
            # Claim first fences registry, then blocks scrubbing writer's row.
            with pytest.raises(ArchiveBlocked, match="unsettled_outbox"):
                await claim_conversation(
                    claimant,
                    fixture.org_id,
                    fixture.conversation_id,
                    archive_id=uuid4(),
                    lease_id=uuid4(),
                    cutoff=datetime.now(timezone.utc) + timedelta(days=1),
                )

        pending = asyncio.create_task(claim())
        try:
            async with fixture.sessions() as monitor:
                for _ in range(200):
                    waiting = await monitor.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted)"
                        ),
                        {"pid": claimant_pid},
                    )
                    if waiting:
                        break
                    await asyncio.sleep(0.005)
                assert waiting and not pending.done()
            # The writer already owns the child, but its guard must not wait on
            # claim's registry lock and complete a registry/child lock cycle.
            with pytest.raises(DBAPIError) as caught:
                await asyncio.wait_for(
                    writer.execute(
                        text(
                            "UPDATE interaction_outbox SET error_code='direct_writer' WHERE id=:id"
                        ),
                        {"id": status_id},
                    ),
                    5,
                )
            assert caught.value.orig.sqlstate == "55000"
            await writer.rollback()
            await asyncio.wait_for(pending, 5)
        finally:
            await writer.rollback()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await claimant.rollback()
