from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from src.core.auth import UserInfo, get_current_user_or_session
from src.core.dependencies import get_service_db_session


class _Session:
    rolled_back = False

    async def rollback(self) -> None:
        self.rolled_back = True


class _Service:
    def __init__(self, turn: SimpleNamespace, error: str | None = None) -> None:
        self.turn = turn
        self.error = error
        self.calls: list[tuple[object, ...]] = []

    async def turn_for_execution(self, session: object, org_id: UUID, execution_id: UUID):  # noqa: ANN201
        return self.turn

    async def execute(self, session, subject, execution_id, invocation_id, tool, args):  # noqa: ANN001, ANN201
        from src.services.image_tools import ImageToolExecutionError

        self.calls.append((execution_id, invocation_id, tool, args))
        if self.error:
            raise ImageToolExecutionError(self.error)
        return {"imageId": str(uuid4()), "contentType": "image/png", "width": 2, "height": 2}


def _identity(*, session: bool = True) -> tuple[UserInfo, UUID]:
    conversation_id = uuid4()
    claims = {"type": "session", "conversation_id": str(conversation_id)} if session else {}
    return (
        UserInfo(user_id=str(uuid4()), org_id=str(uuid4()), role="member", raw_claims=claims),
        conversation_id,
    )


def _client(identity: UserInfo, session: _Session, service: _Service, monkeypatch) -> httpx.AsyncClient:  # noqa: ANN001
    from src.routers import image_tools_internal

    async def db_override():  # noqa: ANN202
        yield session

    async def identity_override() -> UserInfo:
        return identity

    monkeypatch.setattr(image_tools_internal, "image_tools_service", service)
    app = FastAPI()
    app.include_router(image_tools_internal.router, prefix="/v1/internal")
    app.dependency_overrides[get_service_db_session] = db_override
    app.dependency_overrides[get_current_user_or_session] = identity_override
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _url(turn_id: UUID, execution_id: UUID, invocation_id: UUID) -> str:
    return (
        f"/v1/internal/turns/{turn_id}/executions/{execution_id}/"
        f"image-operations/{invocation_id}"
    )


@pytest.fixture(autouse=True)
def _enabled(monkeypatch) -> None:  # noqa: ANN001
    from src.routers import image_tools_internal

    monkeypatch.setattr(image_tools_internal.settings, "IMAGES_ENABLED", True)


async def test_runner_session_executes_its_own_turn(monkeypatch) -> None:  # noqa: ANN001
    identity, conversation_id = _identity()
    turn = SimpleNamespace(id=uuid4(), conversation_id=conversation_id)
    service = _Service(turn)
    execution_id, invocation_id = uuid4(), uuid4()
    async with _client(identity, _Session(), service, monkeypatch) as client:
        response = await client.post(
            _url(turn.id, execution_id, invocation_id),
            json={"tool": "generate_image", "args": {"prompt": "a fox", "size": "1024x1024"}},
        )
    assert response.status_code == 200
    assert response.json()["contentType"] == "image/png"
    assert service.calls == [
        (execution_id, invocation_id, "generate_image", {"prompt": "a fox", "size": "1024x1024"})
    ]


async def test_browser_credentials_cannot_use_the_internal_surface(monkeypatch) -> None:  # noqa: ANN001
    identity, conversation_id = _identity(session=False)
    service = _Service(SimpleNamespace(id=uuid4(), conversation_id=conversation_id))
    async with _client(identity, _Session(), service, monkeypatch) as client:
        response = await client.post(
            _url(uuid4(), uuid4(), uuid4()),
            json={"tool": "generate_image", "args": {"prompt": "x"}},
        )
    assert response.status_code == 403 and service.calls == []


async def test_disabled_operator_switch_rejects_before_execution(monkeypatch) -> None:  # noqa: ANN001
    from src.routers import image_tools_internal

    monkeypatch.setattr(image_tools_internal.settings, "IMAGES_ENABLED", False)
    identity, conversation_id = _identity()
    service = _Service(SimpleNamespace(id=uuid4(), conversation_id=conversation_id))
    async with _client(identity, _Session(), service, monkeypatch) as client:
        response = await client.post(
            _url(uuid4(), uuid4(), uuid4()),
            json={"tool": "generate_image", "args": {"prompt": "x"}},
        )
    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "image_disabled"}
    assert service.calls == []


async def test_a_turn_of_another_conversation_is_not_found(monkeypatch) -> None:  # noqa: ANN001
    identity, _ = _identity()
    turn = SimpleNamespace(id=uuid4(), conversation_id=uuid4())
    service = _Service(turn)
    async with _client(identity, _Session(), service, monkeypatch) as client:
        response = await client.post(
            _url(turn.id, uuid4(), uuid4()),
            json={"tool": "generate_image", "args": {"prompt": "x"}},
        )
    assert response.status_code == 404 and service.calls == []


@pytest.mark.parametrize(
    "args",
    [
        {"tool": "generate_image", "args": {"prompt": ""}},
        {"tool": "generate_image", "args": {"prompt": "x", "image_id": str(uuid4())}},
        {"tool": "edit_image", "args": {"prompt": "x"}},
        {"tool": "edit_image", "args": {"prompt": "x", "image_url": "https://a/b.png"}},
        {"tool": "web_search", "args": {"query": "x"}},
    ],
)
async def test_malformed_requests_never_reach_the_service(monkeypatch, args) -> None:  # noqa: ANN001
    identity, conversation_id = _identity()
    service = _Service(SimpleNamespace(id=uuid4(), conversation_id=conversation_id))
    async with _client(identity, _Session(), service, monkeypatch) as client:
        response = await client.post(_url(uuid4(), uuid4(), uuid4()), json=args)
    assert response.status_code == 422 and service.calls == []


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("image_access_denied", 403),
        ("image_limit", 429),
        ("image_rejected", 422),
        ("image_not_found", 404),
        ("image_cancelled", 409),
        ("image_timeout", 504),
        ("image_unavailable", 503),
        ("something_internal", 503),
    ],
)
async def test_service_errors_map_to_safe_codes(monkeypatch, code: str, status: int) -> None:  # noqa: ANN001
    identity, conversation_id = _identity()
    turn = SimpleNamespace(id=uuid4(), conversation_id=conversation_id)
    session = _Session()
    async with _client(identity, session, _Service(turn, error=code), monkeypatch) as client:
        response = await client.post(
            _url(turn.id, uuid4(), uuid4()),
            json={"tool": "generate_image", "args": {"prompt": "x"}},
        )
    assert response.status_code == status
    expected = code if code != "something_internal" else "image_unavailable"
    assert response.json()["detail"] == {"code": expected}
    assert session.rolled_back
