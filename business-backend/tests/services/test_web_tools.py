import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.core.authorization_types import AuthorizationSubject
from src.core.config import settings
from src.models.conversation import Conversation
from src.models.interaction import InteractionExecution, InteractionTurn, ToolOperation
from src.models.organization import Organization
from src.models.user import User
from src.models.web_source import WebSource
from src.schemas.interaction import TurnLimits
from src.schemas.web_tools import WebErrorCode
from src.services.interaction import InteractionAccessDenied, interaction_service
from src.repositories.interaction_repository import InteractionRepository
from src.repositories.web_source_repository import WebSourceRepository
from src.services.web_tools import (
    WEB_TOOL_ATTEMPT_DURATION_SECONDS,
    WEB_TOOL_ATTEMPTS_TOTAL,
    WEB_TOOL_RESPONSE_BYTES_TOTAL,
    WebToolExecutionError,
    WebToolsService,
    citation_is_owned,
)
from src.services.web_tools_client import WebToolsClientError


@dataclass(frozen=True)
class WebFixture:
    sessions: async_sessionmaker[AsyncSession]
    subject: AuthorizationSubject
    conversation_id: UUID
    turn_id: UUID
    execution_id: UUID


@pytest.fixture
async def web_fixture() -> WebFixture:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    org_id, owner_id, conversation_id, turn_id, execution_id = (uuid4() for _ in range(5))
    try:
        async with sessions.begin() as db:
            db.add(Organization(id=org_id, name="Web service test", slug=f"web-svc-{org_id}"))
            await db.flush()
            db.add(
                User(
                    id=owner_id,
                    org_id=org_id,
                    keycloak_id=uuid4(),
                    email=f"{owner_id}@example.test",
                )
            )
            await db.flush()
            db.add(
                Conversation(
                    id=conversation_id,
                    org_id=org_id,
                    user_id=owner_id,
                    model="fixture",
                )
            )
            await db.flush()
            db.add(
                InteractionTurn(
                    id=turn_id,
                    org_id=org_id,
                    conversation_id=conversation_id,
                    owner_id=owner_id,
                    limits=TurnLimits().model_dump(),
                )
            )
            await db.flush()
            db.add(
                InteractionExecution(
                    id=execution_id,
                    org_id=org_id,
                    turn_id=turn_id,
                    workflow_id=f"conversation-{conversation_id}",
                    run_id=uuid4(),
                    task_queue=f"session-{conversation_id}",
                    phase="ordinary",
                    iteration=1,
                )
            )
        yield WebFixture(
            sessions,
            AuthorizationSubject(owner_id, org_id, "member"),
            conversation_id,
            turn_id,
            execution_id,
        )
    finally:
        async with sessions.begin() as db:
            await db.execute(delete(InteractionTurn).where(InteractionTurn.id == turn_id))
            await db.execute(delete(Conversation).where(Conversation.id == conversation_id))
            await db.execute(delete(User).where(User.id == owner_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
        await engine.dispose()


class FakeClient:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def execute(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, args))
        if self.entered is not None and self.release is not None:
            self.entered.set()
            await self.release.wait()
        return self.result


class FailedClient(FakeClient):
    def __init__(self, code: WebErrorCode) -> None:
        super().__init__({})
        self.code = code

    async def execute(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, args))
        raise WebToolsClientError(self.code, retryable=self.code == "web_timeout")


class RetryClient(FakeClient):
    async def execute(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, args))
        if len(self.calls) == 1:
            raise WebToolsClientError("web_timeout", retryable=True)
        return self.result


def allow_authorization() -> Callable[..., Awaitable[None]]:
    async def allow(*args: object) -> None:
        del args

    return allow


def test_citation_must_belong_to_the_same_org_and_turn() -> None:
    assert citation_is_owned("org-a", "turn-a", "org-a", "turn-a")
    assert not citation_is_owned("org-a", "turn-a", "org-a", "turn-b")
    assert not citation_is_owned("org-a", "turn-a", "org-b", "turn-a")


async def test_real_authorization_releases_turn_lock_before_network_and_cancel_wins(
    web_fixture: WebFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep the real authorization context query and its FOR UPDATE; only the
    # unrelated ACL/model lookup is replaced for this owned fixture.
    monkeypatch.setattr(interaction_service, "_require_live", AsyncMock())
    monkeypatch.setattr(interaction_service._authorization, "require_capability", AsyncMock())
    cancelled = False

    class CancelDuringFetch:
        async def execute(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
            nonlocal cancelled
            async with web_fixture.sessions.begin() as other:
                await other.execute(text("SET LOCAL lock_timeout = '200ms'"))
                await InteractionRepository(web_fixture.subject.org_id).cancel_turn(
                    other, web_fixture.turn_id
                )
            cancelled = True
            return {"results": []}

    async with web_fixture.sessions() as db:
        with pytest.raises(WebToolExecutionError):
            await WebToolsService(client=CancelDuringFetch()).execute(
                db,
                web_fixture.subject,
                web_fixture.execution_id,
                uuid4(),
                "web_search",
                {"query": "public report"},
            )
    assert cancelled, "cancel must finish while the external request is in flight"
    async with web_fixture.sessions() as db:
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == web_fixture.subject.org_id)
            )
        ).one()
        assert operation.status == "cancelled"
        assert operation.result is None


async def test_nonretryable_service_failure_uses_only_one_durable_attempt(
    web_fixture: WebFixture,
) -> None:
    import httpx
    from src.services.web_tools_client import WebToolsClient

    attempts = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            503,
            json={
                "detail": {
                    "code": "web_unavailable",
                    "retryable": False,
                }
            },
        )

    client = WebToolsClient(
        base_url="http://web-tools:8120",
        service_secret="test-only-secret-123",
        transport=httpx.MockTransport(upstream),
    )
    async with web_fixture.sessions() as db:
        with pytest.raises(WebToolExecutionError, match="web_unavailable"):
            await WebToolsService(client=client, authorize=allow_authorization()).execute(
                db,
                web_fixture.subject,
                web_fixture.execution_id,
                uuid4(),
                "web_search",
                {"query": "public report"},
            )
    assert attempts == 1
    async with web_fixture.sessions() as db:
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == web_fixture.subject.org_id)
            )
        ).one()
        assert operation.status == "failed" and operation.attempts == 1


def test_web_metrics_have_only_bounded_non_tenant_dimensions() -> None:
    for metric in (
        WEB_TOOL_ATTEMPTS_TOTAL,
        WEB_TOOL_ATTEMPT_DURATION_SECONDS,
        WEB_TOOL_RESPONSE_BYTES_TOTAL,
    ):
        assert metric._labelnames == ("tool", "status")


async def test_duplicate_invocation_returns_one_platform_result_and_one_source(
    web_fixture: WebFixture,
) -> None:
    client = FakeClient(
        {
            "results": [
                {
                    "title": "Report",
                    "url": "https://example.org/report",
                    "snippet": "Public summary",
                    "published_at": None,
                }
            ]
        }
    )
    service = WebToolsService(client=client, authorize=allow_authorization())
    invocation_id = uuid4()
    args = {"query": "annual report", "language": None, "time_range": None}

    async with web_fixture.sessions() as db:
        first = await service.execute(
            db,
            web_fixture.subject,
            web_fixture.execution_id,
            invocation_id,
            "web_search",
            args,
        )
    async with web_fixture.sessions.begin() as db:
        await db.execute(
            update(ToolOperation)
            .where(ToolOperation.org_id == web_fixture.subject.org_id)
            .values(created_at=text("clock_timestamp() - interval '31 seconds'"))
        )
    async with web_fixture.sessions() as db:
        retry = await service.execute(
            db,
            web_fixture.subject,
            web_fixture.execution_id,
            invocation_id,
            "web_search",
            args,
        )

    assert retry == first
    assert len(client.calls) == 1
    assert UUID(first["results"][0]["sourceId"])
    async with web_fixture.sessions() as db:
        assert (
            len(
                (
                    await db.scalars(
                        select(WebSource).where(WebSource.org_id == web_fixture.subject.org_id)
                    )
                ).all()
            )
            == 1
        )
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == web_fixture.subject.org_id)
            )
        ).one()
        assert operation.status == "completed"
    with pytest.raises(WebToolExecutionError, match="interaction_conflict"):
        async with web_fixture.sessions() as db:
            await service.execute(
                db,
                web_fixture.subject,
                web_fixture.execution_id,
                invocation_id,
                "web_search",
                {**args, "query": "different"},
            )


async def test_operation_in_progress_duplicate_waits_for_same_durable_result(
    web_fixture: WebFixture,
) -> None:
    client = FakeClient({"results": []})
    client.entered, client.release = asyncio.Event(), asyncio.Event()
    service = WebToolsService(client=client, authorize=allow_authorization())
    invocation_id = uuid4()
    args = {"query": "annual report", "language": None, "time_range": None}

    first_session = web_fixture.sessions()
    first = asyncio.create_task(
        service.execute(
            first_session,
            web_fixture.subject,
            web_fixture.execution_id,
            invocation_id,
            "web_search",
            args,
        )
    )
    try:
        await asyncio.wait_for(client.entered.wait(), timeout=2)
        async with web_fixture.sessions() as second_session:
            second = asyncio.create_task(
                service.execute(
                    second_session,
                    web_fixture.subject,
                    web_fixture.execution_id,
                    invocation_id,
                    "web_search",
                    args,
                )
            )
            await asyncio.sleep(0.2)
            assert len(client.calls) == 1
            client.release.set()
            assert await asyncio.wait_for(second, timeout=2) == {"results": []}
        assert await asyncio.wait_for(first, timeout=2) == {"results": []}
        assert len(client.calls) == 1
    finally:
        client.release.set()
        await asyncio.gather(first, return_exceptions=True)
        await first_session.close()


async def test_live_revoke_after_fetch_discards_result_and_source(
    web_fixture: WebFixture,
) -> None:
    client = FakeClient(
        {
            "requested_url": "https://example.org/report",
            "final_url": "https://example.org/report",
            "content_type": "text/html",
            "text": "Fetched but revoked",
            "title": "Report",
            "truncated": False,
            "page_count": None,
        }
    )
    client.entered, client.release = asyncio.Event(), asyncio.Event()
    checks = 0

    async def revoke_on_second_check(*args: object) -> None:
        nonlocal checks
        del args
        checks += 1
        if checks == 3:
            raise InteractionAccessDenied()

    service = WebToolsService(client=client, authorize=revoke_on_second_check)
    async with web_fixture.sessions() as db:
        task = asyncio.create_task(
            service.execute(
                db,
                web_fixture.subject,
                web_fixture.execution_id,
                uuid4(),
                "web_read",
                {"url": "https://example.org/report"},
            )
        )
        await asyncio.wait_for(client.entered.wait(), timeout=2)
        client.release.set()
        with pytest.raises(WebToolExecutionError, match="web_access_denied"):
            await task

    async with web_fixture.sessions() as db:
        assert (
            await db.scalars(
                select(WebSource).where(WebSource.org_id == web_fixture.subject.org_id)
            )
        ).all() == []
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == web_fixture.subject.org_id)
            )
        ).one()
        assert operation.status == "failed"
        assert operation.result is None


async def test_source_history_is_owner_and_exact_turn_scoped(
    web_fixture: WebFixture,
) -> None:
    client = FakeClient(
        {
            "results": [
                {
                    "title": "History",
                    "url": "https://example.org/history",
                    "snippet": "Stored source",
                    "published_at": "2026-09-01T00:00:00Z",
                }
            ]
        }
    )
    service = WebToolsService(client=client, authorize=allow_authorization())
    async with web_fixture.sessions() as db:
        await service.execute(
            db,
            web_fixture.subject,
            web_fixture.execution_id,
            uuid4(),
            "web_search",
            {"query": "report", "language": None, "time_range": None},
        )
    async with web_fixture.sessions() as db:
        sources = await service.list_sources(
            db,
            web_fixture.subject,
            web_fixture.conversation_id,
            web_fixture.turn_id,
        )
        assert len(sources) == 1
        assert sources[0].title == "History"
        assert sources[0].snippet == "Stored source"
        foreign_user = AuthorizationSubject(uuid4(), web_fixture.subject.org_id, "org_admin")
        with pytest.raises(WebToolExecutionError, match="interaction_not_found"):
            await service.list_sources(
                db,
                foreign_user,
                web_fixture.conversation_id,
                web_fixture.turn_id,
            )
        with pytest.raises(WebToolExecutionError, match="interaction_not_found"):
            await service.list_sources(
                db,
                web_fixture.subject,
                web_fixture.conversation_id,
                uuid4(),
            )


@pytest.mark.parametrize(
    ("error_code", "expected_attempts"),
    [("web_timeout", 2), ("unsupported_document", 1)],
)
async def test_allowed_external_failure_is_durable_and_consumes_one_slot(
    web_fixture: WebFixture, error_code: WebErrorCode, expected_attempts: int
) -> None:
    client = FailedClient(error_code)
    service = WebToolsService(client=client, authorize=allow_authorization())
    invocation_id = uuid4()
    for _ in range(2):
        with pytest.raises(WebToolExecutionError, match=error_code):
            async with web_fixture.sessions() as db:
                await service.execute(
                    db,
                    web_fixture.subject,
                    web_fixture.execution_id,
                    invocation_id,
                    "web_read",
                    {"url": "https://example.org/report"},
                )

    assert len(client.calls) == expected_attempts
    async with web_fixture.sessions() as db:
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == web_fixture.subject.org_id)
            )
        ).one()
        turn = await db.get(InteractionTurn, web_fixture.turn_id)
        assert operation.status == "failed" and operation.attempts == expected_attempts
        assert operation.error_code == error_code
        assert turn is not None and turn.read_count == 1


async def test_transient_failure_retries_within_one_durable_two_attempt_budget(
    web_fixture: WebFixture,
) -> None:
    client = RetryClient({"results": []})
    service = WebToolsService(client=client, authorize=allow_authorization())
    invocation_id = uuid4()

    async with web_fixture.sessions() as db:
        result = await service.execute(
            db,
            web_fixture.subject,
            web_fixture.execution_id,
            invocation_id,
            "web_search",
            {"query": "annual report", "language": None, "time_range": None},
        )

    assert result == {"results": []}
    assert len(client.calls) == 2
    async with web_fixture.sessions() as db:
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == web_fixture.subject.org_id)
            )
        ).one()
        assert operation.status == "completed" and operation.attempts == 2


async def test_retry_does_not_reset_durable_operation_deadline(
    web_fixture: WebFixture,
) -> None:
    class ExpireBeforeRetryClient(FakeClient):
        async def execute(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((tool, args))
            if len(self.calls) == 1:
                async with web_fixture.sessions.begin() as other:
                    await other.execute(
                        update(ToolOperation)
                        .where(ToolOperation.org_id == web_fixture.subject.org_id)
                        .values(created_at=text("clock_timestamp() - interval '31 seconds'"))
                    )
                raise WebToolsClientError("web_timeout", retryable=True)
            return self.result

    client = ExpireBeforeRetryClient({"results": []})
    service = WebToolsService(client=client, authorize=allow_authorization())

    with pytest.raises(WebToolExecutionError, match="web_timeout"):
        async with web_fixture.sessions() as db:
            await service.execute(
                db,
                web_fixture.subject,
                web_fixture.execution_id,
                uuid4(),
                "web_search",
                {"query": "annual report", "language": None, "time_range": None},
            )

    assert len(client.calls) == 1
    async with web_fixture.sessions() as db:
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == web_fixture.subject.org_id)
            )
        ).one()
        turn = await db.get(InteractionTurn, web_fixture.turn_id)
        assert operation.status == "failed" and operation.attempts == 1
        assert operation.error_code == "web_timeout"
        assert turn is not None and turn.search_count == 1


async def test_expired_lease_takeover_fails_without_another_request_or_source(
    web_fixture: WebFixture,
) -> None:
    client = FakeClient(
        {
            "results": [
                {
                    "title": "Too late",
                    "url": "https://example.org/late",
                    "snippet": "Must not be saved",
                    "published_at": None,
                }
            ]
        }
    )
    service = WebToolsService(client=client, authorize=allow_authorization())
    invocation_id = uuid4()
    args = {"query": "annual report", "language": None, "time_range": None}
    async with web_fixture.sessions.begin() as db:
        operation = await InteractionRepository(web_fixture.subject.org_id).reserve_operation(
            db,
            web_fixture.execution_id,
            invocation_id,
            "web_search",
            service._arguments_hash(args),
        )
        await WebSourceRepository(web_fixture.subject.org_id).claim_operation(
            db, operation.id, lease_seconds=30
        )
    async with web_fixture.sessions.begin() as db:
        await db.execute(
            update(ToolOperation)
            .where(ToolOperation.org_id == web_fixture.subject.org_id)
            .values(
                created_at=text("clock_timestamp() - interval '31 seconds'"),
                lease_until=text("clock_timestamp() - interval '1 second'"),
            )
        )

    with pytest.raises(WebToolExecutionError, match="web_timeout"):
        async with web_fixture.sessions() as db:
            await service.execute(
                db,
                web_fixture.subject,
                web_fixture.execution_id,
                invocation_id,
                "web_search",
                args,
            )

    assert client.calls == []
    async with web_fixture.sessions() as db:
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == web_fixture.subject.org_id)
            )
        ).one()
        sources = (
            await db.scalars(
                select(WebSource).where(WebSource.org_id == web_fixture.subject.org_id)
            )
        ).all()
        assert operation.status == "failed" and operation.attempts == 1
        assert operation.error_code == "web_timeout"
        assert sources == []


async def test_network_wait_uses_remaining_budget_and_cancels_client(
    web_fixture: WebFixture,
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingClient:
        async def execute(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
            del tool, args
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    service = WebToolsService(client=BlockingClient(), authorize=allow_authorization())
    invocation_id = uuid4()
    args = {"query": "annual report", "language": None, "time_range": None}
    async with web_fixture.sessions.begin() as db:
        await InteractionRepository(web_fixture.subject.org_id).reserve_operation(
            db,
            web_fixture.execution_id,
            invocation_id,
            "web_search",
            service._arguments_hash(args),
        )
    async with web_fixture.sessions.begin() as db:
        await db.execute(
            update(ToolOperation)
            .where(ToolOperation.org_id == web_fixture.subject.org_id)
            .values(created_at=text("clock_timestamp() - interval '29 seconds'"))
        )

    with pytest.raises(WebToolExecutionError, match="web_timeout"):
        async with web_fixture.sessions() as db:
            await asyncio.wait_for(
                service.execute(
                    db,
                    web_fixture.subject,
                    web_fixture.execution_id,
                    invocation_id,
                    "web_search",
                    args,
                ),
                timeout=2.5,
            )

    assert entered.is_set()
    assert cancelled.is_set()
    async with web_fixture.sessions() as db:
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == web_fixture.subject.org_id)
            )
        ).one()
        assert operation.status == "failed"
        assert operation.error_code == "web_timeout"
        assert (
            await db.scalars(
                select(WebSource).where(WebSource.org_id == web_fixture.subject.org_id)
            )
        ).all() == []


async def test_delayed_claim_commit_cannot_restore_expired_network_budget(
    web_fixture: WebFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient({"results": []})
    service = WebToolsService(client=client, authorize=allow_authorization())
    invocation_id = uuid4()
    args = {"query": "annual report", "language": None, "time_range": None}
    async with web_fixture.sessions.begin() as db:
        await InteractionRepository(web_fixture.subject.org_id).reserve_operation(
            db,
            web_fixture.execution_id,
            invocation_id,
            "web_search",
            service._arguments_hash(args),
        )
    async with web_fixture.sessions.begin() as db:
        await db.execute(
            update(ToolOperation)
            .where(ToolOperation.org_id == web_fixture.subject.org_id)
            .values(created_at=text("clock_timestamp() - interval '29 seconds'"))
        )

    async with web_fixture.sessions() as db:
        original_commit = db.commit
        commit_count = 0

        async def delay_first_commit() -> None:
            nonlocal commit_count
            commit_count += 1
            if commit_count == 1:
                await asyncio.sleep(1.25)
            await original_commit()

        monkeypatch.setattr(db, "commit", delay_first_commit)
        with pytest.raises(WebToolExecutionError, match="web_timeout"):
            await service.execute(
                db,
                web_fixture.subject,
                web_fixture.execution_id,
                invocation_id,
                "web_search",
                args,
            )

    assert client.calls == []
    async with web_fixture.sessions() as db:
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == web_fixture.subject.org_id)
            )
        ).one()
        assert operation.status == "failed"
        assert operation.error_code == "web_timeout"


async def test_scrubbed_completed_result_is_not_refetched(
    web_fixture: WebFixture,
) -> None:
    client = FakeClient({"results": []})
    service = WebToolsService(client=client, authorize=allow_authorization())
    invocation_id = uuid4()
    args = {"query": "annual report", "language": None, "time_range": None}
    async with web_fixture.sessions() as db:
        await service.execute(
            db,
            web_fixture.subject,
            web_fixture.execution_id,
            invocation_id,
            "web_search",
            args,
        )
    async with web_fixture.sessions.begin() as db:
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == web_fixture.subject.org_id)
            )
        ).one()
        operation.result = None

    with pytest.raises(WebToolExecutionError, match="web_unavailable"):
        async with web_fixture.sessions() as db:
            await service.execute(
                db,
                web_fixture.subject,
                web_fixture.execution_id,
                invocation_id,
                "web_search",
                args,
            )

    assert len(client.calls) == 1


async def test_expired_source_content_is_hidden_before_physical_sweep(
    web_fixture: WebFixture,
) -> None:
    client = FakeClient(
        {
            "requested_url": "https://example.org/report",
            "final_url": "https://example.org/report",
            "content_type": "text/html",
            "text": "Temporary normalized content",
            "title": "Report",
            "truncated": False,
            "page_count": None,
        }
    )
    service = WebToolsService(client=client, authorize=allow_authorization())
    async with web_fixture.sessions() as db:
        await service.execute(
            db,
            web_fixture.subject,
            web_fixture.execution_id,
            uuid4(),
            "web_read",
            {"url": "https://example.org/report"},
        )
    async with web_fixture.sessions.begin() as db:
        operation = (
            await db.scalars(
                select(ToolOperation).where(ToolOperation.org_id == web_fixture.subject.org_id)
            )
        ).one()
        operation.result_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    async with web_fixture.sessions() as db:
        sources = await service.list_sources(
            db,
            web_fixture.subject,
            web_fixture.conversation_id,
            web_fixture.turn_id,
        )

    assert len(sources) == 1
    assert sources[0].snippet is None
    async with web_fixture.sessions() as db:
        stored = (
            await db.scalars(
                select(WebSource).where(WebSource.org_id == web_fixture.subject.org_id)
            )
        ).one()
        assert stored.content == "Temporary normalized content"


async def test_cancelled_turn_is_reported_as_cancelled_not_an_outage(
    web_fixture: WebFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user cancel must not reach the model as a generic web failure.

    Before this behaviour existed the retried activity saw the turn's generic
    `interaction_conflict`, which the runner's allowlist does not recognize,
    so it fell back to `web_unavailable` — indistinguishable from a real
    outage — while a cancel landing mid-flight surfaced as `web_access_denied`.
    """
    monkeypatch.setattr(interaction_service, "_require_live", AsyncMock())
    monkeypatch.setattr(interaction_service._authorization, "require_capability", AsyncMock())
    invocation_id = uuid4()

    class CancelDuringFetch:
        async def execute(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
            async with web_fixture.sessions.begin() as other:
                await other.execute(text("SET LOCAL lock_timeout = '200ms'"))
                await InteractionRepository(web_fixture.subject.org_id).cancel_turn(
                    other, web_fixture.turn_id
                )
            return {"results": []}

    async with web_fixture.sessions() as db:
        with pytest.raises(WebToolExecutionError) as mid_flight:
            await WebToolsService(client=CancelDuringFetch()).execute(
                db,
                web_fixture.subject,
                web_fixture.execution_id,
                invocation_id,
                "web_search",
                {"query": "public report"},
            )
    assert mid_flight.value.code == "web_cancelled"

    # The runner's activity retries the same invocation after the cancel.
    async with web_fixture.sessions() as db:
        with pytest.raises(WebToolExecutionError) as retried:
            await WebToolsService(client=CancelDuringFetch()).execute(
                db,
                web_fixture.subject,
                web_fixture.execution_id,
                invocation_id,
                "web_search",
                {"query": "public report"},
            )
    assert retried.value.code == "web_cancelled"
