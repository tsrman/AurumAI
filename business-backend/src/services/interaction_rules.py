"""Pure interaction boundaries; clock/ownership are supplied by the transaction owner."""

import hashlib
import json
from datetime import datetime

from src.schemas.interaction import QuestionRequest


def answer_allowed(status: str, turn_active: bool, now: datetime, expires_at: datetime) -> bool:
    return status == "pending" and turn_active and now < expires_at


def question_arguments_hash(request: QuestionRequest) -> str:
    encoded = json.dumps(
        request.model_dump(by_alias=True), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
