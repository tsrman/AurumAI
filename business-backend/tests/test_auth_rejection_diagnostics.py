import json
import time
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, HTTPException
from structlog.testing import capture_logs

from src.core import auth
from src.core.config import settings
from src.core.keycloak import KeycloakClient
from src.middleware.error_handler import setup_exception_handlers


@pytest.fixture
def signing(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = KeycloakClient()
    monkeypatch.setattr(client, "get_signing_key", AsyncMock(return_value=private.public_key()))
    monkeypatch.setattr(auth, "keycloak_client", client)
    return private


@pytest.mark.asyncio
@pytest.mark.parametrize("case,reason", [
    ("iat", "ImmatureSignatureError"), ("nbf", "ImmatureSignatureError"),
    ("expired", "ExpiredSignatureError"), ("signature", "InvalidSignatureError"),
    ("issuer", "InvalidIssuerError"), ("audience", "InvalidAudienceError"),
    ("malformed", "DecodeError"),
    ("future_wrong_issuer", "ImmatureSignatureError"),
    ("future_wrong_audience", "ImmatureSignatureError"),
    ("unknown_kid", "PyJWTError"),
])
async def test_rejected_jwt_logs_only_safe_correlated_diagnostics(signing, case, reason):
    now = int(time.time())
    payload = {"sub": "private-user", "email": "private-email", "iat": now - 10,
               "exp": now + 300, "aud": settings.KEYCLOAK_CLIENT_ID,
               "iss": settings.KEYCLOAK_ISSUER or f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}"}
    if case in {"iat", "nbf"}:
        payload[case] = now + 60
    if case == "expired":
        payload["exp"] = now - 60
    if case in {"future_wrong_issuer", "future_wrong_audience"}:
        payload["iat"] = now + 60
    if case in {"issuer", "future_wrong_issuer"}:
        payload["iss"] = "private-issuer"
    if case in {"audience", "future_wrong_audience"}:
        payload["aud"] = "private-audience"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if case == "signature" else signing
    token = jwt.encode(payload, key, algorithm="RS256", headers={"kid": "private-kid"})
    if case == "malformed":
        token = "private-malformed-token"
    if case == "unknown_kid":
        auth.keycloak_client.get_signing_key.side_effect = jwt.PyJWTError("private-error-text")
    app = FastAPI()
    setup_exception_handlers(app)

    @app.get("/protected")
    async def protected(user=Depends(auth.get_current_user)):
        return {"ok": True}

    with capture_logs() as logs:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/protected", headers={"Authorization": f"Bearer {token}", "X-Request-ID": "private-header"})
    assert response.status_code == 401
    assert response.json()["error"]["message"] == ("Token expired" if case == "expired" else "Invalid token")
    events = [r for r in logs if r["event"] == "keycloak_jwt_rejected"]
    assert len(events) == 1
    event = events[0]
    assert event["reason"] == reason
    assert event["request_id"] == response.json()["meta"]["requestId"]
    assert set(event) <= {"event", "log_level", "reason", "request_id", "iat_delta_seconds", "nbf_delta_seconds", "exp_delta_seconds"}
    if case in {"iat", "nbf"}:
        assert 50 < event[f"{case}_delta_seconds"] <= 60
    elif case == "expired":
        assert -70 < event["exp_delta_seconds"] < -59
    else:
        assert not any(k.endswith("delta_seconds") for k in event)
    rendered = json.dumps(logs)
    assert "private-" not in rendered
    assert token not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("downstream_status", [200, 401])
async def test_valid_session_fallback_does_not_log_keycloak_rejection(signing, monkeypatch, downstream_status):
    monkeypatch.setattr(settings, "SECRET_KEY", "diagnostic-test-session-key-at-least-32-bytes")
    token = jwt.encode({"sub": "session-user", "type": "session", "conversation_id": "conversation"}, settings.SECRET_KEY, algorithm="HS256")
    app = FastAPI()
    setup_exception_handlers(app)

    @app.get("/internal")
    async def internal(user=Depends(auth.get_current_user_or_session)):
        if downstream_status == 401:
            raise HTTPException(401, "Unknown session principal")
        return {"ok": True}

    with capture_logs() as logs:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/internal", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == downstream_status
    assert not logs
