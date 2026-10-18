import asyncio
import logging

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

from src.config import settings

logger = logging.getLogger(__name__)

health_app = FastAPI()

# Set once /bind has assigned this (pool-mode) container to a real
# conversation — src/main.py waits on this before starting the Temporal
# worker. Created lazily so importing this module never requires a running
# event loop.
_bind_event: asyncio.Event | None = None


def get_bind_event() -> asyncio.Event:
    global _bind_event
    if _bind_event is None:
        _bind_event = asyncio.Event()
    return _bind_event


class BindRequest(BaseModel):
    conversation_id: str
    user_id: str
    org_id: str | None = None
    session_token: str
    task_queue: str | None = None


class RefreshTokenRequest(BaseModel):
    session_token: str


@health_app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@health_app.get("/pool-status")
def pool_status() -> dict[str, object]:
    """Lets the pool manager tell an unclaimed container apart from one
    that's already bound, when reconciling its own bookkeeping against
    reality on startup."""
    return {"bound": bool(settings.conversation_id)}


@health_app.post("/bind")
def bind(payload: BindRequest) -> dict[str, str]:
    if settings.conversation_id:
        task_queue = payload.task_queue or f"session-{payload.conversation_id}"
        same_identity = (
            settings.conversation_id == payload.conversation_id
            and settings.user_id == payload.user_id
            and settings.org_id == payload.org_id
            and settings.task_queue == task_queue
        )
        if not same_identity:
            raise HTTPException(status_code=409, detail="Already bound to a conversation")
        settings.session_token = payload.session_token
        logger.info("Reconciled binding for conversation_id=%s", settings.conversation_id)
        get_bind_event().set()
        return {"status": "bound"}
    settings.conversation_id = payload.conversation_id
    settings.user_id = payload.user_id
    settings.org_id = payload.org_id
    settings.session_token = payload.session_token
    settings.task_queue = payload.task_queue or f"session-{payload.conversation_id}"
    logger.info("Bound to conversation_id=%s", settings.conversation_id)
    get_bind_event().set()
    return {"status": "bound"}


@health_app.post("/refresh-token")
def refresh_token(payload: RefreshTokenRequest) -> dict[str, str]:
    """Session tokens (services/session_token.py on the backend) expire
    after 30 minutes, but this container keeps running for as long as the
    conversation stays active — get_session_token (workflows/activities.py)
    always reads the live settings.session_token, so backend's
    SessionManager.get_or_create pushes a fresh one here well before the
    old one expires instead of letting an active chat 401 mid-conversation."""
    if not settings.conversation_id:
        raise HTTPException(status_code=409, detail="Not yet bound to a conversation")
    settings.session_token = payload.session_token
    logger.info("Refreshed session token for conversation_id=%s", settings.conversation_id)
    return {"status": "refreshed"}


def run_health_server(port: int = 8080) -> None:
    uvicorn.run(health_app, host="0.0.0.0", port=port, log_level="warning")
