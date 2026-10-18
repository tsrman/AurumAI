import asyncio
import json
import math
import time
from typing import Any, Optional, cast
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from jwt.algorithms import RSAAlgorithm
from jwt.exceptions import ImmatureSignatureError, InvalidTokenError, PyJWTError
from src.core.config import settings


class KeycloakClient:
    async def rejection_diagnostics(self, token: str, error: PyJWTError) -> dict[str, str | float]:
        """Private, bounded evidence; never used to authorize a request."""
        allowed = {
            "ExpiredSignatureError", "ImmatureSignatureError", "InvalidIssuedAtError",
            "InvalidSignatureError", "InvalidIssuerError", "InvalidAudienceError",
            "InvalidAlgorithmError", "DecodeError", "MissingRequiredClaimError",
        }
        reason = type(error).__name__
        evidence: dict[str, str | float] = {"reason": reason if reason in allowed else "PyJWTError"}
        if not isinstance(error, (jwt.ImmatureSignatureError, jwt.ExpiredSignatureError)):
            return evidence
        try:
            # PyJWT checks dates before issuer/audience. Reverify the signature
            # AND identity constraints before deriving any date diagnostics.
            key = await self.get_signing_key(jwt.get_unverified_header(token)["kid"])
            claims = jwt.decode(
                token, key, algorithms=["RS256"], audience=settings.KEYCLOAK_CLIENT_ID,
                issuer=settings.KEYCLOAK_ISSUER or f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}",
                options={"verify_iat": False, "verify_nbf": False, "verify_exp": False},
            )
            now = time.time()
            for name in ("iat", "nbf", "exp"):
                value = claims.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    delta = float(value) - now
                    if math.isfinite(delta):
                        evidence[f"{name}_delta_seconds"] = round(max(-86400.0, min(86400.0, delta)), 3)
        except (PyJWTError, httpx.HTTPError, KeyError, TypeError, ValueError, OverflowError):
            # Diagnostics must not replace the original authentication failure.
            pass
        return evidence

    def __init__(self) -> None:
        self._jwks: Optional[dict[str, Any]] = None
        self._jwks_fetched_at: float = 0.0
        self._jwks_ttl: int = 86400
        self._reload_lock = asyncio.Lock()

    async def load_jwks(self) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(settings.keycloak_jwks_url)
            resp.raise_for_status()
            self._jwks = resp.json()
            self._jwks_fetched_at = time.time()

    def _find_key(self, kid: str) -> Optional[RSAPublicKey]:
        if not self._jwks:
            return None
        for key in self._jwks.get("keys", []):
            if key.get("kid") == kid:
                # Keycloak's JWKS endpoint only ever publishes public keys —
                # from_jwk()'s wider return type accounts for a JWK carrying
                # private-key components, which never happens here.
                return cast(RSAPublicKey, RSAAlgorithm.from_jwk(json.dumps(key)))
        return None

    def _expired(self) -> bool:
        return not self._jwks or (time.time() - self._jwks_fetched_at) > self._jwks_ttl

    @property
    def jwks_is_fresh(self) -> bool:
        """Check for a live JWKS cache used by /ready.

        A warm cache already validates tokens without a Keycloak request, so
        readiness does not need to contact Keycloak on every probe.
        """
        return not self._expired()

    async def get_signing_key(self, kid: str) -> RSAPublicKey:
        key = None if self._expired() else self._find_key(kid)
        if key is None:
            # Cache is stale (past _jwks_ttl) or `kid` just isn't in it (a
            # Keycloak key rotation mid-TTL) — reload once under a lock so
            # concurrent requests during the reload don't all hit Keycloak at
            # once. Previously this path raised RuntimeError forever once the
            # TTL passed, taking down every authenticated request until a
            # process restart.
            async with self._reload_lock:
                # Another request may have already reloaded while we waited.
                key = None if self._expired() else self._find_key(kid)
                if key is None:
                    await self.load_jwks()
                    key = self._find_key(kid)
        if key is None:
            raise PyJWTError(f"Signing key {kid} not found")
        return key

    async def decode_token(self, token: str) -> dict[str, Any]:
        unverified = jwt.get_unverified_header(token)
        kid = unverified.get("kid")
        if not kid:
            raise PyJWTError("Token header is missing 'kid'")
        key = await self.get_signing_key(kid)
        issuer = settings.KEYCLOAK_ISSUER or f"{settings.KEYCLOAK_URL}/realms/{settings.KEYCLOAK_REALM}"
        # Same claim order as PyJWT (signature, iat/nbf, exp, iss, aud), but
        # only issue/not-before times tolerate clock skew between Keycloak and
        # this API; expiry stays exact.
        unchecked: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            options={
                "verify_iat": False,
                "verify_nbf": False,
                "verify_exp": False,
                "verify_aud": False,
                "verify_iss": False,
            },
        )
        latest_valid_start = time.time() + settings.JWT_CLOCK_SKEW_SECONDS
        for claim in ("iat", "nbf"):
            if claim not in unchecked:
                continue
            try:
                start = int(unchecked[claim])
            except (TypeError, ValueError, OverflowError):
                raise InvalidTokenError(f"Token {claim} claim must be an integer") from None
            if start > latest_valid_start:
                raise ImmatureSignatureError(f"The token is not yet valid ({claim})")
        payload: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=settings.KEYCLOAK_CLIENT_ID,
            issuer=issuer,
            options={"verify_iat": False, "verify_nbf": False},
        )
        return payload


keycloak_client = KeycloakClient()
