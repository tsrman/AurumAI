"""Hermes session tokens: issuance, audience separation and rate-limit identity."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport
from jwt.exceptions import PyJWTError
from starlette.requests import Request

from src.core import auth
from src.core.auth import UserInfo, get_current_user_or_session
from src.core.capability_catalog import Capability
from src.core.config import settings
from src.middleware.rate_limit import _rate_limit_identity
from src.services.hermes_session_token import (
    HERMES_SESSION_AUDIENCE,
    InvalidHermesSessionToken,
    hermes_session_token_service,
)
from src.services.session_token import session_token_service


def _issued():  # noqa: ANN202
    return hermes_session_token_service.issue(
        org_id=uuid.uuid4(), user_id=uuid.uuid4(), conversation_id=uuid.uuid4(), generation=3
    )


def test_issued_token_carries_identity_only_with_its_own_audience() -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=1)
    issued = hermes_session_token_service.issue(
        org_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        generation=3,
        now=now,
    )
    claims = jwt.decode(
        issued.token,
        settings.SECRET_KEY,
        algorithms=["HS256"],
        audience=HERMES_SESSION_AUDIENCE,
        options={"verify_exp": False},
    )

    assert claims["type"] == "hermes_session"
    assert claims["generation"] == 3
    assert claims["sid"] == str(issued.session_id)
    assert issued.expires_at.timestamp() - now.timestamp() == settings.HERMES_SESSION_TOKEN_TTL_SECONDS
    assert "permissions" not in claims and "turn_id" not in claims


def test_runner_session_decoder_rejects_hermes_tokens() -> None:
    with pytest.raises(PyJWTError):
        session_token_service.decode(_issued().token)


def test_hermes_decoder_rejects_runner_tokens() -> None:
    runner = session_token_service.issue(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))

    with pytest.raises(InvalidHermesSessionToken):
        hermes_session_token_service.decode(runner)


@pytest.mark.parametrize(
    "change",
    [{"type": "session"}, {"permissions": ["x"]}, {"generation": 0}, {"sid": "not-a-uuid"}],
)
def test_hermes_decoder_rejects_tampered_claims(change: dict[str, object]) -> None:
    claims = jwt.decode(
        _issued().token, settings.SECRET_KEY, algorithms=["HS256"], audience=HERMES_SESSION_AUDIENCE
    )
    forged = jwt.encode({**claims, **change}, settings.SECRET_KEY, algorithm="HS256")

    with pytest.raises(InvalidHermesSessionToken):
        hermes_session_token_service.decode(forged)


async def test_session_routes_reject_hermes_tokens(monkeypatch) -> None:
    async def reject(*_args: object, **_kwargs: object) -> UserInfo:
        raise HTTPException(status_code=401, detail="Invalid token")

    monkeypatch.setattr(auth, "get_current_user", reject)
    request = Request(
        {"type": "http", "method": "POST", "path": "/internal", "headers": [], "query_string": b"", "path_params": {}}
    )
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=_issued().token)

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user_or_session(request, credentials)

    assert exc_info.value.status_code == 401


async def test_rate_limit_counts_a_hermes_sandbox_against_its_user() -> None:
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    token = hermes_session_token_service.issue(
        org_id=org_id, user_id=user_id, conversation_id=uuid.uuid4(), generation=1
    ).token
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/hermes/chat/completions",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "client": ("127.0.0.1", 1),
        }
    )

    with patch(
        "src.middleware.rate_limit.keycloak_client.decode_token",
        new_callable=AsyncMock,
        side_effect=PyJWTError("not a keycloak token"),
    ):
        assert await _rate_limit_identity(request) == (str(org_id), str(user_id))


async def _mint(env, token: str | None) -> httpx.Response:  # noqa: ANN001
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(transport=ASGITransport(app=env.app), base_url="http://test") as client:
        return await client.post("/v1/internal/hermes/session-tokens", headers=headers)


def _runner_token(env) -> str:  # noqa: ANN001
    return session_token_service.issue(
        str(env.conversation_id), str(env.user_id), str(env.org_id)
    )


async def test_runner_mints_a_token_for_its_active_conversation(hermes_env) -> None:
    response = await _mint(hermes_env, _runner_token(hermes_env))

    assert response.status_code == 201
    body = response.json()
    claims = hermes_session_token_service.decode(body["token"])
    assert claims["conversation_id"] == str(hermes_env.conversation_id)
    assert claims["sub"] == str(hermes_env.user_id)
    assert claims["sid"] == body["sessionId"]
    assert claims["generation"] == 1


async def test_a_hermes_token_cannot_mint_another(hermes_env) -> None:
    hermes_token = hermes_session_token_service.issue(
        org_id=hermes_env.org_id,
        user_id=hermes_env.user_id,
        conversation_id=hermes_env.conversation_id,
        generation=1,
    ).token

    response = await _mint(hermes_env, hermes_token)

    assert response.status_code == 401


async def test_minting_requires_the_capability_and_an_active_turn(hermes_env) -> None:
    await hermes_env.finish_turn()
    inactive = await _mint(hermes_env, _runner_token(hermes_env))
    await hermes_env.revoke(Capability.AGENTS_HERMES)
    denied = await _mint(hermes_env, _runner_token(hermes_env))

    assert inactive.status_code == 403 and "hermes_turn_inactive" in inactive.text
    assert denied.status_code == 403 and "hermes_access_denied" in denied.text


async def test_minting_is_absent_while_the_operator_switch_is_off(hermes_env, monkeypatch) -> None:
    monkeypatch.setattr(settings, "HERMES_ENABLED", False)

    response = await _mint(hermes_env, _runner_token(hermes_env))

    assert response.status_code == 404
