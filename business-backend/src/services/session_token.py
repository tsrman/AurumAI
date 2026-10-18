from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from src.core.config import settings


class SessionTokenService:
    def issue(
        self,
        conversation_id: str,
        user_id: str,
        org_id: str | None,
        *, generation: int = 1,
    ) -> str:
        payload: dict[str, Any] = {
            "sub": user_id,
            "conversation_id": conversation_id,
            "org_id": org_id,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
            "type": "session",
            "generation": generation,
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")

    def decode(self, token: str) -> dict[str, Any]:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])


session_token_service = SessionTokenService()
