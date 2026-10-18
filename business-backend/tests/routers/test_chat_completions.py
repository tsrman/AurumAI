import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from src.core.auth import UserInfo, get_current_user_or_session
from src.core.capability_catalog import Capability
from src.core.authorization_types import AccessPermission, GrantSource, PrincipalType
from src.core.dependencies import get_service_db_session
from src.middleware.error_handler import setup_exception_handlers
from src.models.access_grant import AccessGrant
from src.models.organization import Organization
from src.models.organization_capability import OrganizationCapability
from src.models.user import User
from src.routers import chat
from src.services.quota import (
    QuotaExceededError,
    QuotaInvariantError,
    UsageReservation,
    get_quota_service,
)


class RecordingQuotaService:
    def __init__(self) -> None:
        self.reservations: list[dict[str, object]] = []
        self.accepted: list[uuid.UUID] = []
        self.cancelled: list[uuid.UUID] = []
        self.completed: list[tuple[uuid.UUID, int, int, int]] = []
        self.reserve_error: Exception | None = None
        self.accept_error: Exception | None = None
        self.complete_error: Exception | None = None

    async def reserve(
        self,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        model: str,
        request_id: uuid.UUID,
        **correlation: object,
    ) -> UsageReservation:
        if self.reserve_error is not None:
            raise self.reserve_error
        self.reservations.append(
            {
                "org_id": org_id,
                "user_id": user_id,
                "conversation_id": conversation_id,
                "model": model,
                "request_id": request_id,
                **correlation,
            }
        )
        return UsageReservation(request_id=request_id, period_start=date(2026, 8, 1))

    async def accept(self, org_id: uuid.UUID, request_id: uuid.UUID, **_: object) -> None:
        self.accepted.append(request_id)
        if self.accept_error is not None:
            raise self.accept_error

    async def cancel(self, org_id: uuid.UUID, request_id: uuid.UUID) -> None:
        self.cancelled.append(request_id)

    async def complete(
        self,
        org_id: uuid.UUID,
        request_id: uuid.UUID,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        **_: object,
    ) -> None:
        self.completed.append(
            (request_id, prompt_tokens, completion_tokens, total_tokens)
        )
        if self.complete_error is not None:
            raise self.complete_error


def _make_app(user_override=None, session_override=None, quota_service=None):
    test_app = FastAPI()
    setup_exception_handlers(test_app)
    test_app.include_router(chat.router, prefix="/v1")
    if user_override is not None:
        test_app.dependency_overrides[get_current_user_or_session] = user_override
    if session_override is not None:
        test_app.dependency_overrides[get_service_db_session] = session_override
    test_app.dependency_overrides[get_quota_service] = lambda: (
        quota_service or RecordingQuotaService()
    )
    return test_app


RUNNER_ORG_ID = "11111111-1111-1111-1111-111111111111"
RUNNER_USER_ID = "22222222-2222-2222-2222-222222222222"
RUNNER_CONVERSATION_ID = "33333333-3333-3333-3333-333333333333"


def _runner_session_user() -> UserInfo:
    return UserInfo(
        user_id=RUNNER_USER_ID,
        org_id=RUNNER_ORG_ID,
        role="member",
        raw_claims={
            "type": "session",
            "conversation_id": RUNNER_CONVERSATION_ID,
        },
    )


@pytest.fixture(autouse=True)
def _isolate_non_authorization_tests_from_model_grants(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if request.node.name.startswith(
        "test_internal_completion_callers_require_and_accept_live_v2_model_grants"
    ):
        return
    monkeypatch.setattr(
        "src.routers.chat.ensure_model_allowed",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "src.routers.chat.authorization_service.has_capability",
        AsyncMock(return_value=True),
    )


async def _noop_session():
    yield None


@pytest.mark.asyncio
async def test_chat_completions_requires_auth():
    test_app = _make_app(session_override=_noop_session)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_chat_completions_streams_mock_llm_in_debug_mode(monkeypatch, db):
    monkeypatch.setattr("src.services.llm_client.settings.DEBUG", True)
    monkeypatch.setattr("src.routers.chat.llm_client.api_key", "")

    async def _db_session():
        yield db

    test_app = _make_app(user_override=_runner_session_user, session_override=_db_session)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as client:
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        ) as response:
            assert response.status_code == 200
            body = b"".join([chunk async for chunk in response.aiter_bytes()]).decode()

    assert "data: [DONE]" in body
    assert '"delta"' in body


@pytest.mark.parametrize(
    "model_id",
    [
        "conversation/provider-model",
        "evaluator/provider-model",
        "agent/provider-model",
        "code-skill/provider-model",
    ],
)
@pytest.mark.asyncio
async def test_internal_completion_callers_require_and_accept_live_v2_model_grants(
    model_id: str,
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid.UUID(RUNNER_ORG_ID)
    user_id = uuid.UUID(RUNNER_USER_ID)
    db.add(Organization(id=org_id, name="Runner Org", slug=f"runner-{uuid.uuid4()}"))
    await db.flush()
    db.add(OrganizationCapability(org_id=org_id, capability=Capability.CHAT_SEND.value))
    db.add(
        User(
            id=user_id,
            keycloak_id=uuid.uuid4(),
            email=f"runner-{uuid.uuid4()}@example.com",
            org_id=org_id,
        )
    )
    await db.flush()
    await db.commit()
    async def fake_stream_chat(
        request: dict[str, object],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ):
        if on_accepted is not None:
            await on_accepted()
        yield '{"choices":[{"delta":{"content":"allowed"}}]}'

    async def db_session():
        yield db

    app = _make_app(user_override=_runner_session_user, session_override=db_session)
    with patch("src.routers.chat.llm_client.stream_chat", side_effect=fake_stream_chat):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            denied = await client.post(
                "/v1/chat/completions",
                json={"model": model_id, "messages": [{"role": "user", "content": "hi"}]},
            )
            db.add(
                AccessGrant(
                    org_id=org_id,
                    resource_type="model",
                    resource_id=model_id,
                    principal_type=PrincipalType.USER.value,
                    principal_id=user_id,
                    permission=AccessPermission.READ.value,
                    created_via=GrantSource.PROVISIONING.value,
                )
            )
            await db.commit()
            response = await client.post(
                "/v1/chat/completions",
                json={"model": model_id, "messages": [{"role": "user", "content": "hi"}]},
            )

    assert denied.status_code == 403
    assert denied.json()["error"] == {
        "code": "FORBIDDEN",
        "message": "Model not allowed",
        "details": {},
    }
    assert model_id not in denied.text
    assert response.status_code == 200
    assert "allowed" in response.text


@pytest.mark.asyncio
async def test_chat_completions_forwards_gateway_chunks_as_sse(db):
    async def fake_stream_chat(
        request: dict[str, object],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ):
        if on_accepted is not None:
            await on_accepted()
        yield '{"choices": [{"delta": {"content": "Hi"}}]}'
        yield '{"choices": [{"delta": {"content": " there"}}]}'

    async def _db_session():
        yield db

    test_app = _make_app(user_override=_runner_session_user, session_override=_db_session)
    with patch("src.routers.chat.llm_client.stream_chat", side_effect=fake_stream_chat):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
            )

    body = response.text
    assert 'data: {"choices": [{"delta": {"content": "Hi"}}]}' in body
    assert 'data: {"choices": [{"delta": {"content": " there"}}]}' in body
    assert body.strip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_chat_completions_always_requests_stream_true_upstream(db):
    """Regression test: this endpoint always returns
    a StreamingResponse, but used to forward the client's own `stream`
    field as-is — a client (or a stale/misconfigured caller) sending
    `stream: false` got silently zero SSE lines back (llm_client.stream_chat
    only parses "data: " lines, which a non-streamed gateway response
    doesn't have), not an error."""
    captured_requests = []

    async def fake_stream_chat(
        request: dict[str, object],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ):
        if on_accepted is not None:
            await on_accepted()
        captured_requests.append(request)
        yield '{"choices": [{"delta": {"content": "Hi"}}]}'

    async def _db_session():
        yield db

    test_app = _make_app(user_override=_runner_session_user, session_override=_db_session)
    with patch("src.routers.chat.llm_client.stream_chat", side_effect=fake_stream_chat):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )

    assert len(captured_requests) == 1
    assert captured_requests[0]["stream"] is True


@pytest.mark.asyncio
async def test_chat_completions_gateway_error_mid_stream_yields_clean_error_event(db):
    """Regression test: a gateway failure after the
    stream had already started (200 sent, some SSE chunks flushed) used to
    propagate as a bare exception — connection just dropped, no error event,
    nothing logged server-side, and the runner's own parsing (no "choices"
    key survives) treated it as an empty completion instead of a real
    failure. Now the generator catches it, logs the gateway's actual error
    body, and yields a structured error chunk before the stream's own
    [DONE] terminator (the real HTTP status can't change at this point —
    200 is already on the wire)."""

    async def failing_stream_chat(
        request: dict[str, object],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ):
        if on_accepted is not None:
            await on_accepted()
        yield '{"choices": [{"delta": {"content": "partial"}}]}'
        response = httpx.Response(502, request=httpx.Request("POST", "http://gateway/x"))
        raise httpx.HTTPStatusError("boom", request=response.request, response=response)

    async def _db_session():
        yield db

    test_app = _make_app(user_override=_runner_session_user, session_override=_db_session)
    with patch("src.routers.chat.llm_client.stream_chat", side_effect=failing_stream_chat):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )

    assert response.status_code == 200
    body = response.text
    assert 'data: {"choices": [{"delta": {"content": "partial"}}]}' in body
    assert '"error"' in body
    assert "boom" in body
    assert body.strip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_chat_completions_accounts_the_last_valid_provider_usage(db) -> None:
    quota = RecordingQuotaService()
    captured_request: dict[str, object] = {}

    async def accepted_stream(
        request: dict[str, object],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ):
        captured_request.update(request)
        assert on_accepted is not None
        await on_accepted()
        yield '{"choices":[{"delta":{"content":"Hi"}}]}'
        yield (
            '{"choices":[],"usage":{"prompt_tokens":1,'
            '"completion_tokens":2,"total_tokens":3}}'
        )
        yield '{"choices":[],"usage":{"prompt_tokens":true,"completion_tokens":9,"total_tokens":10}}'
        yield (
            '{"choices":[],"usage":{"prompt_tokens":4,'
            '"completion_tokens":5,"total_tokens":11}}'
        )

    async def _db_session():
        yield db

    test_app = _make_app(
        user_override=_runner_session_user,
        session_override=_db_session,
        quota_service=quota,
    )
    with patch("src.routers.chat.llm_client.stream_chat", side_effect=accepted_stream):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]},
            )

    assert response.status_code == 200
    assert captured_request["stream_options"] == {"include_usage": True}
    assert len(quota.reservations) == 1
    reservation = quota.reservations[0]
    assert reservation["org_id"] == uuid.UUID(RUNNER_ORG_ID)
    assert reservation["user_id"] == uuid.UUID(RUNNER_USER_ID)
    assert reservation["conversation_id"] == uuid.UUID(RUNNER_CONVERSATION_ID)
    assert reservation["model"] == "gpt-test"
    request_id = reservation["request_id"]
    assert isinstance(request_id, uuid.UUID)
    assert quota.accepted == [request_id]
    assert quota.completed == [(request_id, 4, 5, 11)]
    assert quota.cancelled == []
    assert '"usage":{"prompt_tokens":4' in response.text
    assert response.text.strip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_chat_completions_correlates_workflow_usage_without_forwarding_fields(
    db,
) -> None:
    quota = RecordingQuotaService()
    captured_request: dict[str, object] = {}
    run_id = uuid.uuid4()

    async def accepted_stream(
        request: dict[str, object],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ):
        captured_request.update(request)
        assert on_accepted is not None
        await on_accepted()
        yield '{"choices":[{"delta":{"content":"done"}}]}'

    async def db_session():
        yield db

    app = _make_app(
        user_override=_runner_session_user,
        session_override=db_session,
        quota_service=quota,
    )
    with patch("src.routers.chat.llm_client.stream_chat", side_effect=accepted_stream):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "gpt-test",
                    "messages": [{"role": "user", "content": "hi"}],
                    "workflow_run_id": str(run_id),
                    "workflow_phase": "node",
                    "workflow_node_key": "research",
                },
            )

    assert response.status_code == 200
    assert quota.reservations[0]["workflow_run_id"] == run_id
    assert quota.reservations[0]["workflow_phase"] == "node"
    assert quota.reservations[0]["workflow_node_key"] == "research"
    assert "workflow_run_id" not in captured_request
    assert "workflow_phase" not in captured_request
    assert "workflow_node_key" not in captured_request


@pytest.mark.asyncio
async def test_chat_completions_rejects_incomplete_workflow_correlation(db) -> None:
    async def db_session():
        yield db

    app = _make_app(user_override=_runner_session_user, session_override=db_session)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-test",
                "messages": [{"role": "user", "content": "hi"}],
                "workflow_phase": "node",
                "workflow_node_key": "research",
            },
        )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_chat_completions_keeps_an_accepted_request_without_valid_usage(db) -> None:
    quota = RecordingQuotaService()

    async def accepted_stream(
        request: dict[str, object],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ):
        assert on_accepted is not None
        await on_accepted()
        yield '{"choices":[],"usage":{"prompt_tokens":-1,"completion_tokens":2,"total_tokens":1}}'
        yield '{"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":"2","total_tokens":3}}'

    async def _db_session():
        yield db

    test_app = _make_app(
        user_override=_runner_session_user,
        session_override=_db_session,
        quota_service=quota,
    )
    with patch("src.routers.chat.llm_client.stream_chat", side_effect=accepted_stream):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]},
            )

    assert response.status_code == 200
    assert len(quota.accepted) == 1
    assert quota.completed == []
    assert quota.cancelled == []


@pytest.mark.asyncio
async def test_chat_completions_cancels_a_reservation_before_gateway_acceptance(db) -> None:
    quota = RecordingQuotaService()

    async def rejected_stream(
        request: dict[str, object],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ):
        response = httpx.Response(502, request=httpx.Request("POST", "http://gateway/x"))
        raise httpx.HTTPStatusError("boom", request=response.request, response=response)
        yield  # pragma: no cover

    async def _db_session():
        yield db

    test_app = _make_app(
        user_override=_runner_session_user,
        session_override=_db_session,
        quota_service=quota,
    )
    with patch("src.routers.chat.llm_client.stream_chat", side_effect=rejected_stream):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]},
            )

    assert response.status_code == 200
    request_id = quota.reservations[0]["request_id"]
    assert quota.accepted == []
    assert quota.completed == []
    assert quota.cancelled == [request_id]


@pytest.mark.asyncio
async def test_chat_completions_returns_real_429_before_opening_the_stream(db) -> None:
    quota = RecordingQuotaService()
    quota.reserve_error = QuotaExceededError(
        "requests", used=10, limit=10, period_end=date(2026, 9, 1)
    )
    stream_started = False

    async def must_not_start(
        request: dict[str, object],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ):
        nonlocal stream_started
        stream_started = True
        yield "unexpected"

    async def _db_session():
        yield db

    test_app = _make_app(
        user_override=_runner_session_user,
        session_override=_db_session,
        quota_service=quota,
    )
    with patch("src.routers.chat.llm_client.stream_chat", side_effect=must_not_start):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]},
            )

    assert response.status_code == 429
    assert response.json()["error"] == {
        "code": "quota_exceeded",
        "message": "Monthly quota exceeded",
        "details": {
            "resource": "requests",
            "used": 10,
            "limit": 10,
            "periodEnd": "2026-09-01",
        },
    }
    assert stream_started is False


@pytest.mark.asyncio
async def test_provider_acceptance_failure_never_refunds_the_request(db) -> None:
    quota = RecordingQuotaService()
    quota.accept_error = RuntimeError("quota acceptance unavailable")

    async def accepted_stream(
        request: dict[str, object],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ):
        assert on_accepted is not None
        await on_accepted()
        yield '{"choices":[{"delta":{"content":"must not escape"}}]}'

    with patch("src.routers.chat.ensure_model_allowed", new_callable=AsyncMock):
        with patch("src.routers.chat.llm_client.stream_chat", side_effect=accepted_stream):
            response = await chat.chat_completions(
                request=chat.ChatCompletionRequest(
                    model="gpt-test",
                    messages=[chat.ChatCompletionMessage(role="user", content="hi")],
                ),
                http_request=MagicMock(),
                user=_runner_session_user(),
                session=db,
                quota=quota,
            )
            with pytest.raises(RuntimeError, match="quota acceptance unavailable"):
                _ = [chunk async for chunk in response.body_iterator]

    assert len(quota.accepted) == 1
    assert quota.cancelled == []


@pytest.mark.asyncio
async def test_client_cancellation_before_acceptance_runs_shielded_cleanup(db) -> None:
    quota = RecordingQuotaService()

    async def cancelled_stream(
        request: dict[str, object],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ):
        raise asyncio.CancelledError
        yield  # pragma: no cover

    with patch("src.routers.chat.ensure_model_allowed", new_callable=AsyncMock):
        with patch("src.routers.chat.llm_client.stream_chat", side_effect=cancelled_stream):
            response = await chat.chat_completions(
                request=chat.ChatCompletionRequest(
                    model="gpt-test",
                    messages=[chat.ChatCompletionMessage(role="user", content="hi")],
                ),
                http_request=MagicMock(),
                user=_runner_session_user(),
                session=db,
                quota=quota,
            )
            with pytest.raises(asyncio.CancelledError):
                _ = [chunk async for chunk in response.body_iterator]

    assert len(quota.cancelled) == 1


@pytest.mark.asyncio
async def test_null_usage_chunks_are_not_reported_as_invalid(db, caplog) -> None:
    """With stream_options an OpenAI-compatible provider sends `"usage": null`
    on every chunk and the real figures only on the last one. Treating the
    null as malformed logged one warning per token of every stream."""
    quota = RecordingQuotaService()

    async def accepted_stream(
        request: dict[str, object],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ):
        assert on_accepted is not None
        await on_accepted()
        for word in ("Hi", " there", "!"):
            yield '{"choices":[{"delta":{"content":"%s"}}],"usage":null}' % word
        yield (
            '{"choices":[],"usage":{"prompt_tokens":7,'
            '"completion_tokens":3,"total_tokens":10}}'
        )

    async def _db_session():
        yield db

    test_app = _make_app(
        user_override=_runner_session_user,
        session_override=_db_session,
        quota_service=quota,
    )
    with caplog.at_level(logging.WARNING, logger="src.routers.chat"):
        with patch("src.routers.chat.llm_client.stream_chat", side_effect=accepted_stream):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/v1/chat/completions",
                    json={"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]},
                )

    assert response.status_code == 200
    assert "invalid LLM usage" not in caplog.text
    request_id = quota.reservations[0]["request_id"]
    assert quota.completed == [(request_id, 7, 3, 10)]


@pytest.mark.asyncio
async def test_completion_failure_does_not_truncate_a_delivered_stream(db, caplog) -> None:
    """Every content chunk has already reached the runner by the time
    complete() runs. An accounting failure there used to escape the generator,
    skipping both the Langfuse span end and `[DONE]` and leaving the runner
    with a truncated transport for a request that actually succeeded."""
    quota = RecordingQuotaService()
    quota.complete_error = QuotaInvariantError("quota event has conflicting completion usage")

    async def accepted_stream(
        request: dict[str, object],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ):
        assert on_accepted is not None
        await on_accepted()
        yield '{"choices":[{"delta":{"content":"Hi"}}]}'
        yield (
            '{"choices":[],"usage":{"prompt_tokens":1,'
            '"completion_tokens":2,"total_tokens":3}}'
        )

    async def _db_session():
        yield db

    test_app = _make_app(
        user_override=_runner_session_user,
        session_override=_db_session,
        quota_service=quota,
    )
    with caplog.at_level(logging.ERROR, logger="src.routers.chat"):
        with patch("src.routers.chat.llm_client.stream_chat", side_effect=accepted_stream):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/v1/chat/completions",
                    json={"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]},
                )

    assert response.status_code == 200
    assert '"delta":{"content":"Hi"}' in response.text
    assert response.text.strip().endswith("data: [DONE]")
    assert "quota completion failed" in caplog.text
    # An accepted request is never refunded, failed accounting included.
    assert quota.cancelled == []


@pytest.mark.asyncio
async def test_accepted_stream_without_any_usage_is_logged_as_unaccounted(db, caplog) -> None:
    """A gateway that drops stream_options leaves the event accepted with zero
    tokens forever. The platform deliberately does not estimate them
    (docs: quota-tracking design), so the only remaining obligation is that it
    is visible."""
    quota = RecordingQuotaService()

    async def accepted_stream(
        request: dict[str, object],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ):
        assert on_accepted is not None
        await on_accepted()
        yield '{"choices":[{"delta":{"content":"Hi"}}]}'

    async def _db_session():
        yield db

    test_app = _make_app(
        user_override=_runner_session_user,
        session_override=_db_session,
        quota_service=quota,
    )
    with caplog.at_level(logging.WARNING, logger="src.routers.chat"):
        with patch("src.routers.chat.llm_client.stream_chat", side_effect=accepted_stream):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/v1/chat/completions",
                    json={"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]},
                )

    assert response.status_code == 200
    assert quota.completed == []
    assert "tokens unaccounted" in caplog.text
    assert str(quota.reservations[0]["request_id"]) in caplog.text


@pytest.mark.asyncio
async def test_stream_that_never_reached_the_gateway_logs_nothing_unaccounted(db, caplog) -> None:
    """The DEBUG mock stream never contacts a gateway, so by design it never
    reports acceptance and its reservation is cancelled. There is nothing
    unaccounted to warn about — otherwise the default local stack would log a
    line for every single request."""
    quota = RecordingQuotaService()

    async def mock_like_stream(
        request: dict[str, object],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ):
        yield '{"choices":[{"delta":{"content":"Hi"}}]}'

    async def _db_session():
        yield db

    test_app = _make_app(
        user_override=_runner_session_user,
        session_override=_db_session,
        quota_service=quota,
    )
    with caplog.at_level(logging.WARNING, logger="src.routers.chat"):
        with patch("src.routers.chat.llm_client.stream_chat", side_effect=mock_like_stream):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/v1/chat/completions",
                    json={"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]},
                )

    assert response.status_code == 200
    assert "tokens unaccounted" not in caplog.text
    assert quota.cancelled == [quota.reservations[0]["request_id"]]
