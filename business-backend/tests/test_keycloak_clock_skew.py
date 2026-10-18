import time
from unittest.mock import AsyncMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from src.core.config import Settings, settings
from src.core.keycloak import KeycloakClient


@pytest.fixture
def client_and_key(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = KeycloakClient()
    monkeypatch.setattr(client, "get_signing_key", AsyncMock(return_value=private.public_key()))
    return client, private


def _token(private, iat_offset: int) -> str:
    now = int(time.time())
    payload = {
        "sub": "user", "iat": now + iat_offset, "exp": now + 300, "aud": settings.KEYCLOAK_CLIENT_ID,
        "iss": settings.KEYCLOAK_ISSUER or f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}",
    }
    return jwt.encode(payload, private, algorithm="RS256", headers={"kid": "kid"})


@pytest.mark.asyncio
async def test_fresh_token_survives_small_clock_skew_between_keycloak_and_api(client_and_key):
    # Keycloak and the API run on different clocks (hosts or a VM whose clock
    # is stepped); a token issued "a few seconds in the future" is still valid.
    client, private = client_and_key
    payload = await client.decode_token(_token(private, iat_offset=5))
    assert payload["sub"] == "user"


@pytest.mark.asyncio
async def test_token_issued_far_in_the_future_is_still_rejected(client_and_key):
    client, private = client_and_key
    with pytest.raises(jwt.ImmatureSignatureError):
        await client.decode_token(_token(private, iat_offset=120))


def test_clock_skew_tolerance_defaults_to_thirty_seconds():
    assert Settings.model_fields["JWT_CLOCK_SKEW_SECONDS"].default == 30


@pytest.mark.asyncio
async def test_clock_skew_tolerance_never_extends_token_expiry(client_and_key):
    # Natural expiry is an acceptance contract: an expired access token must be
    # rejected immediately, so the skew allowance covers iat/nbf only.
    client, private = client_and_key
    now = int(time.time())
    payload = {
        "sub": "user", "iat": now - 300, "exp": now - 1, "aud": settings.KEYCLOAK_CLIENT_ID,
        "iss": settings.KEYCLOAK_ISSUER or f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}",
    }
    token = jwt.encode(payload, private, algorithm="RS256", headers={"kid": "kid"})
    with pytest.raises(jwt.ExpiredSignatureError):
        await client.decode_token(token)


@pytest.mark.asyncio
async def test_not_before_within_skew_is_accepted(client_and_key):
    client, private = client_and_key
    now = int(time.time())
    payload = {
        "sub": "user", "iat": now, "nbf": now + 5, "exp": now + 300, "aud": settings.KEYCLOAK_CLIENT_ID,
        "iss": settings.KEYCLOAK_ISSUER or f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}",
    }
    token = jwt.encode(payload, private, algorithm="RS256", headers={"kid": "kid"})
    assert (await client.decode_token(token))["sub"] == "user"
