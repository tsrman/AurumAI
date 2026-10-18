import asyncio
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.config import settings
from src.models.conversation import Conversation
from src.models.interaction import InteractionExecution, InteractionTurn
from src.models.interaction import ToolOperation
from src.models.organization import Organization
from src.models.user import User
from src.models.web_source import WebSource
from src.repositories.interaction_repository import InteractionRepository
from src.repositories.web_source_repository import WebSourceRepository, WebSourceRepositoryError
from src.schemas.interaction import TurnLimits


@dataclass(frozen=True)
class RepositoryFixture:
    sessions: async_sessionmaker[AsyncSession]
    org: UUID
    owner: UUID
    conversation: UUID
    turn: UUID
    executions: list[UUID]


@pytest.fixture
async def owned() -> RepositoryFixture:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    org, owner, conversation, turn = (uuid4() for _ in range(4))
    executions = [uuid4(), uuid4()]
    try:
        async with sessions.begin() as db:
            db.add(Organization(id=org, name="Web repository test", slug=f"web-repo-{org}"))
            await db.flush()
            db.add(User(id=owner, org_id=org, keycloak_id=uuid4(), email=f"{owner}@example.test"))
            await db.flush()
            db.add(Conversation(id=conversation, org_id=org, user_id=owner, model="fixture"))
            await db.flush()
            db.add(
                InteractionTurn(
                    id=turn,
                    org_id=org,
                    conversation_id=conversation,
                    owner_id=owner,
                    limits=TurnLimits().model_dump(),
                )
            )
            await db.flush()
            for index, execution in enumerate(executions):
                db.add(
                    InteractionExecution(
                        id=execution,
                        org_id=org,
                        turn_id=turn,
                        workflow_id=f"web-child-{index}",
                        run_id=uuid4(),
                        task_queue="fixture",
                        phase="node",
                        node_key=f"node-{index}",
                        iteration=1,
                    )
                )
        yield RepositoryFixture(sessions, org, owner, conversation, turn, executions)
    finally:
        async with sessions.begin() as db:
            await db.execute(delete(InteractionTurn).where(InteractionTurn.id == turn))
            await db.execute(delete(Conversation).where(Conversation.id == conversation))
            await db.execute(delete(User).where(User.id == owner))
            await db.execute(delete(Organization).where(Organization.id == org))
        await engine.dispose()


async def test_claim_is_leased_and_takeover_never_resets_attempts(owned) -> None:
    interaction = InteractionRepository(owned.org)
    repository = WebSourceRepository(owned.org)
    async with owned.sessions.begin() as db:
        operation = await interaction.reserve_operation(
            db, owned.executions[0], uuid4(), "web_read", "a" * 64
        )
        first = await repository.claim_operation(db, operation.id, lease_seconds=30)
        assert first.attempt == 1
        with pytest.raises(WebSourceRepositoryError, match="operation_in_progress"):
            await repository.claim_operation(db, operation.id, lease_seconds=30)

    async with owned.sessions.begin() as db:
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == owned.org)
            )
        ).one()
        operation.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)

    async with owned.sessions.begin() as db:
        second = await repository.claim_operation(db, operation.id, lease_seconds=30)
        assert second.attempt == 2

    async with owned.sessions.begin() as db:
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == owned.org)
            )
        ).one()
        operation.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    async with owned.sessions.begin() as db:
        with pytest.raises(WebSourceRepositoryError, match="web_unavailable"):
            await repository.claim_operation(db, operation.id, lease_seconds=30)
    async with owned.sessions() as db:
        failed = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == owned.org)
            )
        ).one()
        assert failed.status == "failed" and failed.attempts == 2


async def test_completed_sources_are_org_turn_operation_immutable(owned) -> None:
    interaction = InteractionRepository(owned.org)
    repository = WebSourceRepository(owned.org)
    async with owned.sessions.begin() as db:
        operation = await interaction.reserve_operation(
            db, owned.executions[0], uuid4(), "web_search", "a" * 64
        )
        claim = await repository.claim_operation(db, operation.id)
    async with owned.sessions.begin() as db:
        saved = await repository.complete_operation(
            db,
            operation.id,
            claim.lease_id,
            {
                "results": [
                    {
                        "title": "Report",
                        "url": "https://example.org/report",
                        "snippet": "Summary",
                        "published_at": None,
                    }
                ]
            },
        )

    source_id = saved["results"][0]["sourceId"]
    async with owned.sessions() as db:
        source = (
            await db.scalars(select(WebSource).where(WebSource.org_id == owned.org))
        ).one()
        assert str(source.id) == source_id
        assert source.org_id == owned.org
        assert source.turn_id == owned.turn
        assert source.execution_id == owned.executions[0]
        assert source.operation_id == operation.id
        assert source.content == "Summary"
        assert (await repository.list_sources(db, owned.turn))[0].source.id == source.id
        assert await repository.owned_source_ids(
            db,
            owned.conversation,
            owned.owner,
            owned.turn,
            (source_id, str(uuid4())),
        ) == {source_id}
        with pytest.raises(WebSourceRepositoryError, match="interaction_not_found"):
            await repository.owned_source_ids(
                db,
                owned.conversation,
                owned.owner,
                uuid4(),
                (source_id,),
            )
        with pytest.raises(WebSourceRepositoryError, match="interaction_not_found"):
            await repository.owned_source_ids(
                db,
                uuid4(),
                owned.owner,
                owned.turn,
                (source_id,),
            )
        assert await WebSourceRepository(uuid4()).list_sources(db, owned.turn) == []


async def test_search_result_accepts_an_empty_upstream_snippet(owned) -> None:
    interaction = InteractionRepository(owned.org)
    repository = WebSourceRepository(owned.org)
    async with owned.sessions.begin() as db:
        operation = await interaction.reserve_operation(
            db, owned.executions[0], uuid4(), "web_search", "a" * 64
        )
        claim = await repository.claim_operation(db, operation.id)
    async with owned.sessions.begin() as db:
        saved = await repository.complete_operation(
            db,
            operation.id,
            claim.lease_id,
            {
                "results": [
                    {
                        "title": "No summary",
                        "url": "https://example.org/no-summary",
                        "snippet": "",
                        "published_at": None,
                    }
                ]
            },
        )

    assert saved["results"][0]["snippet"] == ""


async def test_expired_cache_scrubs_content_but_keeps_source_metadata(owned) -> None:
    interaction = InteractionRepository(owned.org)
    repository = WebSourceRepository(owned.org)
    async with owned.sessions.begin() as db:
        operation = await interaction.reserve_operation(
            db, owned.executions[0], uuid4(), "web_read", "a" * 64
        )
        claim = await repository.claim_operation(db, operation.id)
    async with owned.sessions.begin() as db:
        await repository.complete_operation(
            db,
            operation.id,
            claim.lease_id,
            {
                "requested_url": "https://example.org/report",
                "final_url": "https://example.org/report",
                "content_type": "text/html",
                "text": "Temporary normalized content",
                "title": "Report",
                "truncated": False,
                "page_count": None,
            },
        )
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == owned.org)
            )
        ).one()
        operation.result_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    async with owned.sessions.begin() as db:
        assert await repository.scrub_expired_results(db, batch_size=10) == 1
    async with owned.sessions() as db:
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == owned.org)
            )
        ).one()
        source = (
            await db.scalars(select(WebSource).where(WebSource.org_id == owned.org))
        ).one()
        assert operation.result is None
        assert source.content is None
        assert source.title == "Report"
        assert source.final_url == "https://example.org/report"


async def test_terminal_turn_sets_completed_web_cache_expiry_to_24_hours(owned) -> None:
    interaction = InteractionRepository(owned.org)
    repository = WebSourceRepository(owned.org)
    async with owned.sessions.begin() as db:
        operation = await interaction.reserve_operation(
            db, owned.executions[0], uuid4(), "web_search", "a" * 64
        )
        claim = await repository.claim_operation(db, operation.id)
    async with owned.sessions.begin() as db:
        await repository.complete_operation(db, operation.id, claim.lease_id, {"results": []})
        await interaction.finish_turn(db, owned.turn, "completed")

    async with owned.sessions() as db:
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == owned.org)
            )
        ).one()
        turn = await db.get(InteractionTurn, owned.turn)
        assert turn is not None and turn.terminal_at is not None
        assert operation.result_expires_at is not None
        assert operation.result_expires_at - turn.terminal_at == timedelta(hours=24)


async def test_concurrent_siblings_cannot_take_the_same_last_search_slot(owned) -> None:
    async with owned.sessions.begin() as db:
        turn = await db.get(InteractionTurn, owned.turn)
        assert turn is not None
        turn.search_count = 9

    async def reserve(execution_id: UUID) -> str:
        try:
            async with owned.sessions.begin() as db:
                await InteractionRepository(owned.org).reserve_operation(
                    db, execution_id, uuid4(), "web_search", "a" * 64
                )
            return "reserved"
        except ValueError as exc:
            assert str(exc) == "tool_limit"
            return "limited"

    outcomes = await asyncio.wait_for(
        asyncio.gather(*(reserve(execution) for execution in owned.executions)),
        timeout=10,
    )

    assert sorted(outcomes) == ["limited", "reserved"]
    async with owned.sessions() as db:
        turn = await db.get(InteractionTurn, owned.turn)
        assert turn is not None and turn.search_count == 10


async def test_concurrent_siblings_cannot_take_the_same_last_read_slot(owned) -> None:
    async with owned.sessions.begin() as db:
        turn = await db.get(InteractionTurn, owned.turn)
        assert turn is not None
        turn.read_count = 19

    async def reserve(execution_id: UUID) -> str:
        try:
            async with owned.sessions.begin() as db:
                await InteractionRepository(owned.org).reserve_operation(
                    db, execution_id, uuid4(), "web_read", "a" * 64
                )
            return "reserved"
        except ValueError as exc:
            assert str(exc) == "tool_limit"
            return "limited"

    outcomes = await asyncio.wait_for(
        asyncio.gather(*(reserve(execution) for execution in owned.executions)),
        timeout=10,
    )

    assert sorted(outcomes) == ["limited", "reserved"]
    async with owned.sessions() as db:
        turn = await db.get(InteractionTurn, owned.turn)
        assert turn is not None and turn.read_count == 20


async def test_application_role_rls_and_immutable_provenance(owned) -> None:
    interaction = InteractionRepository(owned.org)
    repository = WebSourceRepository(owned.org)
    async with owned.sessions.begin() as db:
        operation = await interaction.reserve_operation(
            db, owned.executions[0], uuid4(), "web_search", "a" * 64
        )
        claim = await repository.claim_operation(db, operation.id)
    async with owned.sessions.begin() as db:
        await repository.complete_operation(
            db,
            operation.id,
            claim.lease_id,
            {
                "results": [
                    {
                        "title": "Report",
                        "url": "https://example.org/report",
                        "snippet": "Summary",
                        "published_at": None,
                    }
                ]
            },
        )

    async with owned.sessions.begin() as db:
        await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
        await db.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(owned.org)},
        )
        assert len((await db.scalars(select(WebSource))).all()) == 1
        await db.execute(
            update(WebSource)
            .where(WebSource.org_id == owned.org)
            .values(content=None)
        )

    with pytest.raises(DBAPIError, match="provenance is immutable"):
        async with owned.sessions.begin() as db:
            await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
            await db.execute(
                text("SELECT set_config('app.current_org_id', :org, true)"),
                {"org": str(owned.org)},
            )
            await db.execute(
                update(WebSource)
                .where(WebSource.org_id == owned.org)
                .values(final_url="https://attacker.invalid/")
            )

    async with owned.sessions.begin() as db:
        await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
        await db.execute(
            text("SELECT set_config('app.current_org_id', :org, true)"),
            {"org": str(uuid4())},
        )
        assert (await db.scalars(select(WebSource))).all() == []


async def test_global_sweeper_scrubs_expired_content_through_security_definer(owned) -> None:
    interaction = InteractionRepository(owned.org)
    repository = WebSourceRepository(owned.org)
    async with owned.sessions.begin() as db:
        operation = await interaction.reserve_operation(
            db, owned.executions[0], uuid4(), "web_read", "a" * 64
        )
        claim = await repository.claim_operation(db, operation.id)
    async with owned.sessions.begin() as db:
        await repository.complete_operation(
            db,
            operation.id,
            claim.lease_id,
            {
                "requested_url": "https://example.org/report",
                "final_url": "https://example.org/report",
                "content_type": "text/html",
                "text": "Expiring content",
                "title": "Report",
                "truncated": False,
                "page_count": None,
            },
        )
        saved = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == owned.org)
            )
        ).one()
        saved.result_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.flush()
        # Keep expiry and scrub in one transaction so a running local backend
        # maintenance loop cannot win the race against this assertion.
        await db.execute(text("SET LOCAL ROLE aurum_ai_app"))
        await db.execute(text("SELECT set_config('app.current_org_id', '', true)"))
        assert await db.scalar(text("SELECT public.scrub_expired_web_results(10)")) == 1
    async with owned.sessions() as db:
        saved = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == owned.org)
            )
        ).one()
        source = (
            await db.scalars(select(WebSource).where(WebSource.org_id == owned.org))
        ).one()
        assert saved.result is None
        assert source.content is None
