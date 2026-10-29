from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from src.core.auth import UserInfo, get_current_user_or_session
from src.core.dependencies import get_service_db_session
from src.schemas.interaction import AnswerRequest, QuestionResponse


class _Session:
    committed = False
    rolled_back = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _Service:
    def __init__(self) -> None:
        self.calls = []
        self.question = QuestionResponse(
            question_id=uuid4(),
            turn_id=uuid4(),
            text="Which city?",
            options=[],
            status="answered",
            version=2,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            created_at=datetime.now(UTC),
            answer={"text": "BKK"},
            answered_at=datetime.now(UTC),
            phase="ordinary",
            node_key=None,
            agent_name=None,
        )

    async def register_execution(
        self,
        session,
        subject,
        conversation_id,
        turn_id,
        data,  # noqa: ANN001
    ):
        self.calls.append(("register", subject, conversation_id, turn_id, data))
        return SimpleNamespace(id=uuid4(), turn_id=turn_id, **data.model_dump())

    async def create_question(
        self,
        session,
        subject,
        conversation_id,
        execution_id,
        data,  # noqa: ANN001
        *,
        allow_new=True,  # noqa: ANN001
    ):
        self.calls.append(
            ("create", subject, conversation_id, execution_id, data, allow_new)
        )
        return self.question

    async def get_question(self, session, subject, conversation_id, question_id):  # noqa: ANN001
        self.calls.append(("get", subject, conversation_id, question_id))
        return self.question

    async def consume_question(
        self,
        session,
        subject,
        conversation_id,
        question_id,  # noqa: ANN001
    ):
        self.calls.append(("consume", subject, conversation_id, question_id))
        return AnswerRequest(text="BKK")

    async def expire_question(
        self,
        session,
        subject,
        conversation_id,
        question_id,  # noqa: ANN001
    ):
        self.calls.append(("expire", subject, conversation_id, question_id))
        return self.question

    async def finish_turn(
        self,
        session,
        org_id,
        owner_id,
        conversation_id,
        turn_id,
        *,
        status,  # noqa: ANN001
    ):
        self.calls.append(("fail", owner_id, conversation_id, turn_id, status, org_id))


def _app(identity, session, service, monkeypatch):  # noqa: ANN001
    from src.routers import questions_internal

    async def db_override():
        yield session

    async def identity_override():
        return identity

    app = FastAPI()
    app.include_router(questions_internal.router, prefix="/v1/internal")
    app.dependency_overrides[get_current_user_or_session] = identity_override
    app.dependency_overrides[get_service_db_session] = db_override
    monkeypatch.setattr(questions_internal, "interaction_service", service)
    return app


def _identity(*, session=True):  # noqa: ANN001
    org_id, user_id, conversation_id = uuid4(), uuid4(), uuid4()
    claims = {"type": "session", "conversation_id": str(conversation_id)} if session else {}
    return UserInfo(user_id=str(user_id), org_id=str(org_id), role="member", raw_claims=claims)


async def test_internal_routes_reject_browser_identity(monkeypatch) -> None:
    app = _app(_identity(session=False), _Session(), _Service(), monkeypatch)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/internal/turns/{uuid4()}/executions",
            json={
                "workflowId": "conversation-test",
                "runId": str(uuid4()),
                "taskQueue": "session-test",
                "phase": "ordinary",
                "iteration": 1,
            },
        )
    assert response.status_code == 403


async def test_session_registers_creates_reads_consumes_and_expires(monkeypatch) -> None:
    identity, session, service = _identity(), _Session(), _Service()
    app = _app(identity, session, service, monkeypatch)
    turn_id, execution_id, invocation_id = uuid4(), uuid4(), uuid4()
    payload = {
        "workflowId": "conversation-test",
        "runId": str(uuid4()),
        "taskQueue": "session-test",
        "phase": "ordinary",
        "iteration": 1,
    }
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        registered = await client.post(f"/v1/internal/turns/{turn_id}/executions", json=payload)
        created = await client.post(
            f"/v1/internal/executions/{execution_id}/questions",
            json={"invocationId": str(invocation_id), "question": {"text": "Which city?"}},
        )
        fetched = await client.get(f"/v1/internal/questions/{service.question.question_id}")
        consumed = await client.post(
            f"/v1/internal/questions/{service.question.question_id}/consume"
        )
        expired = await client.post(f"/v1/internal/questions/{service.question.question_id}/expire")
        failed = await client.post(f"/v1/internal/turns/{turn_id}/fail")

    assert [r.status_code for r in (registered, created, fetched, consumed, expired, failed)] == [
        201,
        201,
        200,
        200,
        200,
        204,
    ]
    assert registered.json()["data"]["workflowId"] == "conversation-test"
    assert created.json()["data"]["questionId"] == str(service.question.question_id)
    assert consumed.json()["data"] == {"text": "BKK", "optionId": None}
    assert session.committed is True
    assert [call[0] for call in service.calls] == [
        "register",
        "create",
        "get",
        "consume",
        "expire",
        "fail",
    ]
    assert all(call[2] == UUID(identity.raw_claims["conversation_id"]) for call in service.calls)


async def test_session_token_cannot_address_another_conversation(monkeypatch) -> None:
    from src.services.interaction import InteractionNotFound

    identity, session, service = _identity(), _Session(), _Service()

    async def denied(*args, **kwargs):  # noqa: ANN002, ANN003
        raise InteractionNotFound()

    service.register_execution = denied
    app = _app(identity, session, service, monkeypatch)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/internal/turns/{uuid4()}/executions",
            json={
                "workflowId": "conversation-foreign",
                "runId": str(uuid4()),
                "taskQueue": "session-foreign",
                "phase": "ordinary",
                "iteration": 1,
            },
        )
    assert response.status_code == 404


@pytest.mark.parametrize("production_handler", [False, True])
async def test_question_limit_returns_stable_internal_error_code(monkeypatch, production_handler) -> None:
    from src.services.interaction import InteractionConflict

    identity, session, service = _identity(), _Session(), _Service()

    async def limited(*args, **kwargs):  # noqa: ANN002, ANN003
        raise InteractionConflict("user_input_limit")

    service.create_question = limited
    app = _app(identity, session, service, monkeypatch)
    if production_handler:
        from src.middleware.error_handler import setup_exception_handlers

        setup_exception_handlers(app)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/internal/executions/{uuid4()}/questions",
            json={"invocationId": str(uuid4()), "question": {"text": "Which city?"}},
        )

    assert response.status_code == 409
    envelope = response.json()
    detail = envelope["error"]["message"] if production_handler else envelope["detail"]
    assert detail == {"code": "user_input_limit"}
    assert session.rolled_back is True
    assert session.committed is False


async def test_feature_flag_is_forwarded_to_question_reservation(monkeypatch) -> None:
    from src.routers import questions_internal

    identity, session, service = _identity(), _Session(), _Service()
    monkeypatch.setattr(questions_internal.settings, "USER_QUESTIONS_ENABLED", False)
    app = _app(identity, session, service, monkeypatch)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/internal/executions/{uuid4()}/questions",
            json={"invocationId": str(uuid4()), "question": {"text": "Which city?"}},
        )

    assert response.status_code == 201
    assert service.calls[-1][0] == "create"
    assert service.calls[-1][-1] is False


async def test_a_failed_turn_keeps_its_reason_as_a_failed_assistant_message(monkeypatch) -> None:
    from src.routers import questions_internal

    identity, session, service = _identity(), _Session(), _Service()
    app = _app(identity, session, service, monkeypatch)
    kept: list[dict] = []

    async def upsert(session, **values):  # noqa: ANN001, ANN003, ANN202
        kept.append(values)

    monkeypatch.setattr(questions_internal.message_repository, "upsert_assistant_by_turn", upsert)
    turn_id, other_turn = uuid4(), uuid4()
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with_reason = await client.post(
            f"/v1/internal/turns/{turn_id}/fail", json={"reason": "Could not get a model response. Reason: x"}
        )
        without = await client.post(f"/v1/internal/turns/{other_turn}/fail")

    assert (with_reason.status_code, without.status_code) == (204, 204)
    [message] = kept
    assert message["turn_id"] == turn_id and message["role"] == "assistant"
    assert message["content"] == "Could not get a model response. Reason: x"
    assert message["extra"] == {"failed": True}
    assert message["conversation_id"] == identity.raw_claims["conversation_id"]
