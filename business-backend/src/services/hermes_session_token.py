"""Session tokens for Hermes Agent sandboxes (docs/HERMES_AGENT.md).

A Hermes sandbox gives the model a terminal, so its token must be useless
outside the two Hermes endpoints: it has its own type and audience, which the
runner's session decoder rejects, and it carries identity only. Whether it may
be used right now (its open session row on the same token version, the turn
bound to it, live capabilities and grants) is decided per request, never from
the token.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import jwt

from src.core.config import settings

HERMES_SESSION_TOKEN_TYPE = "hermes_session"
HERMES_SESSION_AUDIENCE = "hermes-session"
_REQUIRED_CLAIMS = ("sub", "org_id", "conversation_id", "sid", "tv", "generation", "exp", "aud")


class InvalidHermesSessionToken(Exception):
    pass


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


@dataclass(frozen=True, slots=True)
class IssuedHermesSessionToken:
    token: str
    session_id: UUID
    expires_at: datetime


class HermesSessionTokenService:
    def issue(
        self,
        *,
        org_id: UUID,
        user_id: UUID,
        conversation_id: UUID,
        generation: int,
        session_id: UUID,
        token_version: int,
        expires_at: datetime,
        now: datetime | None = None,
    ) -> IssuedHermesSessionToken:
        issued_at = now or datetime.now(timezone.utc)
        payload: dict[str, Any] = {
            "sub": str(user_id),
            "org_id": str(org_id),
            "conversation_id": str(conversation_id),
            "sid": str(session_id),
            "tv": token_version,
            "generation": generation,
            "type": HERMES_SESSION_TOKEN_TYPE,
            "aud": HERMES_SESSION_AUDIENCE,
            "iat": issued_at,
            "exp": expires_at,
        }
        token = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")
        return IssuedHermesSessionToken(token, session_id, expires_at)

    def decode(self, token: str) -> dict[str, Any]:
        """Verified claims; raises ExpiredSignatureError or InvalidHermesSessionToken."""
        try:
            payload = jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=["HS256"],
                audience=HERMES_SESSION_AUDIENCE,
                options={"require": list(_REQUIRED_CLAIMS)},
            )
        except jwt.ExpiredSignatureError:
            raise
        except jwt.PyJWTError as exc:
            raise InvalidHermesSessionToken() from exc
        if (
            payload.get("type") != HERMES_SESSION_TOKEN_TYPE
            or "permissions" in payload
            or not all(_positive_int(payload.get(claim)) for claim in ("generation", "tv"))
        ):
            raise InvalidHermesSessionToken()
        try:
            for claim in ("sub", "org_id", "conversation_id", "sid"):
                UUID(str(payload[claim]))
        except ValueError as exc:
            raise InvalidHermesSessionToken() from exc
        return payload


hermes_session_token_service = HermesSessionTokenService()
