from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import ValidationError

from src.core.auth import UserInfo, get_current_user_or_session
from src.core.dependencies import get_service_db_session
from src.schemas.web_tools import WebOperationRequest, WebReadRequest, WebSearchRequest


class _Session:
    rolled_back = False

    async def rollback(self) -> None:
        self.rolled_back = True


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def authorize_web_operation(
        self,
        session: object,
        subject: object,
        conversation_id: UUID,
        turn_id: UUID,
        execution_id: UUID,
    ) -> None:
        self.calls.append((subject, conversation_id, turn_id, execution_id))


class _Executor:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def execute(
        self,
        session: object,
        subject: object,
        execution_id: UUID,
        invocation_id: UUID,
        tool: str,
        args: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append((session, subject, execution_id, invocation_id, tool, args))
        return {"results": []}


class _FailedExecutor(_Executor):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code

    async def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        from src.services.web_tools import WebToolExecutionError

        del args, kwargs
        raise WebToolExecutionError(self.code)


def _identity(*, session: bool = True, role: str = "member") -> UserInfo:
    org_id, user_id, conversation_id = uuid4(), uuid4(), uuid4()
    claims = {"type": "session", "conversation_id": str(conversation_id)} if session else {}
    return UserInfo(
        user_id=str(user_id),
        org_id=str(org_id),
        role=role,
        raw_claims=claims,
    )


def _app(
    identity: UserInfo,
    session: _Session,
    service: _Service,
    monkeypatch,  # noqa: ANN001
    executor: _Executor | None = None,
) -> FastAPI:
    from src.routers import web_tools_internal

    async def db_override():  # noqa: ANN202
        yield session

    async def identity_override() -> UserInfo:
        return identity

    app = FastAPI()
    app.include_router(web_tools_internal.router, prefix="/v1/internal")
    app.dependency_overrides[get_current_user_or_session] = identity_override
    app.dependency_overrides[get_service_db_session] = db_override
    monkeypatch.setattr(web_tools_internal, "interaction_service", service)
    monkeypatch.setattr(web_tools_internal, "web_tools_service", executor or _Executor())
    return app


def test_model_cannot_supply_http_headers() -> None:
    with pytest.raises(ValidationError):
        WebReadRequest(url="https://example.org", headers={"Cookie": "secret"})


def test_web_tool_schemas_are_strict_and_bounded() -> None:
    search = WebSearchRequest(query="release notes", language="en", time_range="week")
    read = WebReadRequest(url="https://example.org/report.pdf")

    assert search.query == "release notes"
    assert read.url == "https://example.org/report.pdf"
    with pytest.raises(ValidationError):
        WebSearchRequest(query="x" * 2001)
    with pytest.raises(ValidationError):
        WebSearchRequest(query="ok", time_range="decade")
    with pytest.raises(ValidationError):
        WebOperationRequest(tool="web_search", args={"url": "https://example.org"})
    with pytest.raises(ValidationError):
        WebOperationRequest(tool="web_read", args={"query": "wrong shape"})


async def _post(app: FastAPI, turn_id: UUID | None = None) -> httpx.Response:
    turn_id = turn_id or uuid4()
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            f"/v1/internal/turns/{turn_id}/executions/{uuid4()}/web-operations/{uuid4()}",
            json={"tool": "web_search", "args": {"query": "release notes"}},
        )


async def test_browser_identity_is_rejected(monkeypatch) -> None:
    from src.routers import web_tools_internal

    monkeypatch.setattr(web_tools_internal.settings, "WEB_ENABLED", True)
    response = await _post(
        _app(_identity(session=False), _Session(), _Service(), monkeypatch)
    )

    assert response.status_code == 403


async def test_invalid_session_identity_is_unauthorized(monkeypatch) -> None:
    from src.routers import web_tools_internal

    identity = _identity()
    identity.raw_claims["conversation_id"] = "not-a-uuid"
    monkeypatch.setattr(web_tools_internal.settings, "WEB_ENABLED", True)
    response = await _post(_app(identity, _Session(), _Service(), monkeypatch))

    assert response.status_code == 401


@pytest.mark.parametrize("role", ["member", "org_admin"])
async def test_operator_switch_blocks_every_role_before_service(
    role: str, monkeypatch
) -> None:
    from src.routers import web_tools_internal

    service = _Service()
    monkeypatch.setattr(web_tools_internal.settings, "WEB_ENABLED", False)
    response = await _post(_app(_identity(role=role), _Session(), service, monkeypatch))

    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "web_disabled"}
    assert service.calls == []


async def test_live_authorized_operation_executes_durable_web_service(
    monkeypatch,
) -> None:
    from src.routers import web_tools_internal

    identity, session, service, executor = _identity(), _Session(), _Service(), _Executor()
    monkeypatch.setattr(web_tools_internal.settings, "WEB_ENABLED", True)
    response = await _post(_app(identity, session, service, monkeypatch, executor))

    assert response.status_code == 200
    assert response.json() == {"results": []}
    assert len(service.calls) == 1
    assert service.calls[0][1] == UUID(identity.raw_claims["conversation_id"])
    assert len(executor.calls) == 1


@pytest.mark.parametrize(
    ("error_name", "expected_status", "expected_code"),
    [
        ("InteractionNotFound", 404, None),
        ("InteractionAccessDenied", 403, "web_access_denied"),
        ("InteractionConflict", 409, "interaction_conflict"),
    ],
)
async def test_gate_errors_are_stable(
    error_name: str,
    expected_status: int,
    expected_code: str | None,
    monkeypatch,
) -> None:
    from src.routers import web_tools_internal
    from src.services import interaction as interaction_module

    error_type = getattr(interaction_module, error_name)
    service = _Service()

    async def denied(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise error_type()

    service.authorize_web_operation = denied  # type: ignore[method-assign]
    monkeypatch.setattr(web_tools_internal.settings, "WEB_ENABLED", True)
    response = await _post(_app(_identity(), _Session(), service, monkeypatch))

    assert response.status_code == expected_status
    if expected_code is None:
        assert response.json()["detail"] == "Interaction not found"
    else:
        assert response.json()["detail"] == {"code": expected_code}


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("web_access_denied", 403),
        ("web_limit", 429),
        ("web_timeout", 504),
        ("web_too_large", 413),
        ("web_url_blocked", 422),
        ("unsupported_document", 422),
        ("web_unavailable", 503),
    ],
)
async def test_execution_errors_have_stable_http_mapping(
    code: str,
    expected_status: int,
    monkeypatch,
) -> None:
    from src.routers import web_tools_internal

    monkeypatch.setattr(web_tools_internal.settings, "WEB_ENABLED", True)
    response = await _post(
        _app(
            _identity(),
            _Session(),
            _Service(),
            monkeypatch,
            _FailedExecutor(code),
        )
    )

    assert response.status_code == expected_status
    assert response.json()["detail"] == {"code": code}
