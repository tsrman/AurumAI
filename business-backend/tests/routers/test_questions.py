from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import ValidationError

from src.core.auth import UserInfo, get_current_user
from src.core.config import settings
from src.core.dependencies import get_org_db_session
from src.core.keycloak import keycloak_client
from src.schemas.interaction import AnswerRequest


class _Session:
    committed = False
    rolled_back = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _Service:
    def __init__(self, question) -> None:
        self.question = question
        self.calls = []
        self.error = None

    async def list_questions(self, session, subject, conversation_id):  # noqa: ANN001
        self.calls.append(("list", subject, conversation_id))
        if self.error:
            raise self.error
        return [self.question]

    async def answer_question(
        self,
        session,
        subject,
        conversation_id,
        question_id,
        key,
        answer,  # noqa: ANN001
    ):
        self.calls.append(("answer", subject, conversation_id, question_id, key, answer))
        if self.error:
            raise self.error
        return self.question

    async def cancel_turn(self, session, subject, conversation_id, turn_id):  # noqa: ANN001
        self.calls.append(("cancel", subject, conversation_id, turn_id))
        if self.error:
            raise self.error
        return [SimpleNamespace(workflow_id="conversation-test", run_id=uuid4())]

    async def signal_turn_cancel(self, targets, turn_id, conversation_id):  # noqa: ANN001
        self.calls.append(("signal", targets, turn_id, conversation_id))


def _question(owner_id):  # noqa: ANN001
    from src.schemas.interaction import QuestionResponse

    now = datetime.now(UTC)
    return QuestionResponse(
        question_id=uuid4(),
        turn_id=uuid4(),
        text="Which city?",
        options=[{"id": "bkk", "label": "Bangkok"}],
        status="pending",
        version=1,
        expires_at=now + timedelta(hours=1),
        created_at=now,
        phase="ordinary",
        node_key=None,
        agent_name=None,
        answer=None,
        answered_at=None,
    )


def _app(identity, session, service, monkeypatch):  # noqa: ANN001
    from src.routers import questions

    async def db_override():
        yield session

    app = FastAPI()
    app.include_router(questions.router, prefix="/v1")
    app.dependency_overrides[get_current_user] = lambda: identity
    app.dependency_overrides[get_org_db_session] = db_override
    monkeypatch.setattr(questions, "interaction_service", service)
    return app


def test_answer_does_not_accept_runtime_target() -> None:
    with pytest.raises(ValidationError):
        AnswerRequest(text="answer", execution_id=str(uuid4()))


async def test_owner_lists_answers_and_cancels_exact_turn(monkeypatch) -> None:
    org_id, user_id, conversation_id = uuid4(), uuid4(), uuid4()
    question = _question(user_id)
    service = _Service(question)
    session = _Session()
    identity = UserInfo(user_id=str(user_id), org_id=str(org_id), role="member")
    key = uuid4()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=_app(identity, session, service, monkeypatch)),
        base_url="http://test",
    ) as client:
        listed = await client.get(f"/v1/conversations/{conversation_id}/questions")
        answered = await client.post(
            f"/v1/conversations/{conversation_id}/questions/{question.question_id}/answer",
            json={"idempotencyKey": str(key), "answer": {"optionId": "bkk"}},
        )
        cancelled = await client.post(
            f"/v1/conversations/{conversation_id}/turns/{question.turn_id}/cancel"
        )

    assert listed.status_code == answered.status_code == 200
    assert cancelled.status_code == 204
    assert listed.json()["data"][0]["questionId"] == str(question.question_id)
    created_at = datetime.fromisoformat(
        listed.json()["data"][0]["createdAt"].replace("Z", "+00:00")
    )
    assert created_at == question.created_at
    assert answered.json()["data"]["status"] == "pending"
    assert session.committed is True
    assert [call[0] for call in service.calls] == ["list", "answer", "cancel", "signal"]
    assert service.calls[-1][2] == question.turn_id
    assert service.calls[-1][3] == conversation_id


@pytest.mark.parametrize(
    "error_name,status_code",
    [("InteractionNotFound", 404), ("InteractionAccessDenied", 403), ("InteractionConflict", 409)],
)
async def test_owner_api_hides_missing_resources_and_maps_live_denial(
    error_name, status_code, monkeypatch
) -> None:
    from src.services import interaction

    org_id, user_id, conversation_id = uuid4(), uuid4(), uuid4()
    service = _Service(_question(user_id))
    service.error = getattr(interaction, error_name)()
    app = _app(
        UserInfo(user_id=str(user_id), org_id=str(org_id), role="member"),
        _Session(),
        service,
        monkeypatch,
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/v1/conversations/{conversation_id}/questions")
    assert response.status_code == status_code


def test_questions_are_mounted_only_on_business_plane() -> None:
    from src.main_admin import app as admin_app
    from src.main_business import app as business_app

    def paths(app):  # noqa: ANN001
        return set(app.openapi()["paths"])

    assert "/v1/conversations/{conversation_id}/questions" in paths(business_app)
    assert "/v1/conversations/{conversation_id}/questions" not in paths(admin_app)


@pytest.mark.parametrize("audience,expected", [("business-ui", 200), ("admin-ui", 401)])
async def test_questions_enforce_real_business_audience(
    audience, expected, monkeypatch
) -> None:
    from src.routers import questions

    org_id, user_id, conversation_id = uuid4(), uuid4(), uuid4()
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(settings, "KEYCLOAK_CLIENT_ID", "business-ui")

    async def signing_key(kid):  # noqa: ANN001
        assert kid == "questions-test"
        return private.public_key()

    async def db_override():
        yield _Session()

    monkeypatch.setattr(keycloak_client, "get_signing_key", signing_key)
    monkeypatch.setattr(questions, "interaction_service", _Service(_question(user_id)))
    app = FastAPI()
    app.include_router(questions.router, prefix="/v1")
    app.dependency_overrides[get_org_db_session] = db_override
    claims = {
        "sub": str(user_id),
        "org_id": str(org_id),
        "role": "member",
        "iss": settings.KEYCLOAK_ISSUER
        or f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        "aud": audience,
    }
    token = jwt.encode(claims, private, algorithm="RS256", headers={"kid": "questions-test"})
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get(
            f"/v1/conversations/{conversation_id}/questions",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == expected, response.text
