"""Previews of the apps Hermes Agent runs in its sandbox (docs/HERMES_AGENT.md).

Hermes starts an app on a port of its sandbox and shares it with the bridge's
`share_preview` tool. The conversation's owner opens it at the preview's own
origin, never the platform's: the UI asks for a one-time ticket, which the
preview service exchanges for a cookie of that origin and checks, through the
backend, on every new connection. Tickets and grants are tokens of their own
type and audience, useless anywhere else, carrying identity only; whether a
preview may be reached right now (its open session, the owner, both Hermes
capabilities, the conversation's generation) is decided per check.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.auth import UserInfo
from src.core.authorization_types import AuthorizationSubject
from src.core.config import settings
from src.models.data_identity import ConversationIdentity
from src.models.hermes_preview import HermesPreview, HermesPreviewTicket
from src.models.hermes_session import HermesSession

PREVIEW_AUDIENCE = "hermes-preview"
TICKET_TYPE = "hermes_preview_ticket"
GRANT_TYPE = "hermes_preview_grant"
TICKET_SECONDS = 60
GRANT_SECONDS = 3600
_REQUIRED_CLAIMS = ("sub", "org_id", "conversation_id", "generation", "pid", "exp", "aud")


class PreviewError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SharePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: int = Field(ge=1024, le=65535, strict=True)


@dataclass(frozen=True, slots=True)
class PreviewTarget:
    """What the preview service connects to: a sandbox and a port in it."""

    session_id: UUID
    port: int


def enabled() -> bool:
    return settings.HERMES_ENABLED and settings.HERMES_PREVIEW_ENABLED and "{preview}" in settings.HERMES_PREVIEW_ORIGIN


def preview_origin(preview_id: UUID) -> str:
    return settings.HERMES_PREVIEW_ORIGIN.format(preview=preview_id.hex).rstrip("/")


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _token(kind: str, claims: dict[str, Any], seconds: int, now: datetime | None) -> str:
    issued = _now(now)
    payload = {**claims, "type": kind, "aud": PREVIEW_AUDIENCE, "iat": issued, "exp": issued + timedelta(seconds=seconds)}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def _decode(token: str, kind: str, preview_label: str) -> dict[str, Any]:
    try:
        claims = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=["HS256"],
            audience=PREVIEW_AUDIENCE,
            options={"require": list(_REQUIRED_CLAIMS)},
        )
    except jwt.PyJWTError:
        raise PreviewError("hermes_preview_denied") from None
    # A token opens only the preview whose origin it was presented at.
    if claims.get("type") != kind or UUID(str(claims["pid"])).hex != preview_label:
        raise PreviewError("hermes_preview_denied")
    return claims


def claims_user(claims: dict[str, Any]) -> UserInfo:
    """The identity a ticket or grant carries, for the conversation fence."""
    return UserInfo(
        user_id=str(claims["sub"]),
        org_id=str(claims["org_id"]),
        role="member",
        raw_claims={"conversation_id": str(claims["conversation_id"]), "generation": claims["generation"]},
    )


async def share_preview(
    session: AsyncSession, subject: AuthorizationSubject, row: HermesSession, request: SharePreviewRequest
) -> dict[str, Any]:
    """Record the port for the session's bound turn: sharing it again moves
    it to the current turn, so the answer that shared it last shows it."""
    if not enabled():
        raise PreviewError("hermes_preview_disabled")
    assert row.active_turn_id is not None
    preview = await session.scalar(
        select(HermesPreview).where(
            HermesPreview.org_id == subject.org_id,
            HermesPreview.session_id == row.id,
            HermesPreview.port == request.port,
        )
    )
    if preview is None:
        preview = HermesPreview(
            id=uuid4(),
            org_id=subject.org_id,
            session_id=row.id,
            conversation_id=row.conversation_id,
            owner_id=subject.user_id,
            turn_id=row.active_turn_id,
            port=request.port,
        )
        session.add(preview)
    else:
        preview.turn_id = row.active_turn_id
    await session.flush()
    preview_id, port = preview.id, preview.port
    await session.commit()
    return {
        "previewId": str(preview_id),
        "port": port,
        "note": "The user opens it from a card below your answer while this session stays open; "
        "keep the app running and still give them the project as a ZIP.",
    }


async def _live(session: AsyncSession, org_id: UUID, owner_id: UUID, conversation_id: UUID, preview_id: UUID) -> PreviewTarget:
    row = await session.execute(
        select(HermesPreview.session_id, HermesPreview.port, HermesSession.state, HermesSession.expires_at)
        .join(
            HermesSession,
            (HermesSession.org_id == HermesPreview.org_id) & (HermesSession.id == HermesPreview.session_id),
        )
        .where(
            HermesPreview.org_id == org_id,
            HermesPreview.id == preview_id,
            HermesPreview.owner_id == owner_id,
            HermesPreview.conversation_id == conversation_id,
        )
    )
    found = row.one_or_none()
    if found is None:
        raise PreviewError("hermes_preview_denied")
    session_id, port, state, expires_at = found
    if state != "open" or expires_at <= _now(None):
        raise PreviewError("hermes_preview_closed")
    return PreviewTarget(session_id, port)


async def issue_ticket(
    session: AsyncSession, subject: AuthorizationSubject, conversation_id: UUID, preview_id: UUID
) -> str:
    """The URL that opens the preview for its owner: its origin and a one-time ticket."""
    if not enabled():
        raise PreviewError("hermes_preview_disabled")
    await _live(session, subject.org_id, subject.user_id, conversation_id, preview_id)
    generation = await session.scalar(
        select(ConversationIdentity.generation).where(
            ConversationIdentity.org_id == subject.org_id, ConversationIdentity.id == conversation_id
        )
    )
    if generation is None:
        raise PreviewError("hermes_preview_denied")
    ticket_id = uuid4()
    session.add(
        HermesPreviewTicket(
            id=ticket_id,
            org_id=subject.org_id,
            preview_id=preview_id,
            expires_at=_now(None) + timedelta(seconds=TICKET_SECONDS),
        )
    )
    await session.commit()
    ticket = _token(
        TICKET_TYPE,
        {
            "sub": str(subject.user_id),
            "org_id": str(subject.org_id),
            "conversation_id": str(conversation_id),
            "generation": int(generation),
            "pid": str(preview_id),
            "jti": str(ticket_id),
        },
        TICKET_SECONDS,
        None,
    )
    return f"{preview_origin(preview_id)}/__aurum/enter?ticket={ticket}"


def ticket_claims(token: str, preview_label: str) -> dict[str, Any]:
    return _decode(token, TICKET_TYPE, preview_label)


def grant_claims(token: str, preview_label: str) -> dict[str, Any]:
    return _decode(token, GRANT_TYPE, preview_label)


async def redeem(session: AsyncSession, claims: dict[str, Any]) -> tuple[str, PreviewTarget]:
    """Use the ticket once and give the preview's grant."""
    org_id, preview_id = UUID(str(claims["org_id"])), UUID(str(claims["pid"]))
    try:
        ticket_id = UUID(str(claims.get("jti", "")))
    except ValueError:
        raise PreviewError("hermes_preview_denied") from None
    used = await session.scalar(
        update(HermesPreviewTicket)
        .where(
            HermesPreviewTicket.org_id == org_id,
            HermesPreviewTicket.id == ticket_id,
            HermesPreviewTicket.preview_id == preview_id,
            HermesPreviewTicket.used_at.is_(None),
            HermesPreviewTicket.expires_at > _now(None),
        )
        .values(used_at=_now(None))
        .returning(HermesPreviewTicket.id)
    )
    if used is None:
        await session.rollback()
        raise PreviewError("hermes_preview_denied")
    target = await check(session, claims)
    await session.commit()
    grant = _token(
        GRANT_TYPE,
        {key: claims[key] for key in ("sub", "org_id", "conversation_id", "generation", "pid")},
        GRANT_SECONDS,
        None,
    )
    return grant, target


async def check(session: AsyncSession, claims: dict[str, Any]) -> PreviewTarget:
    """Whether the preview may be reached now, and where."""
    if not enabled():
        raise PreviewError("hermes_preview_disabled")
    return await _live(
        session,
        UUID(str(claims["org_id"])),
        UUID(str(claims["sub"])),
        UUID(str(claims["conversation_id"])),
        UUID(str(claims["pid"])),
    )


async def turn_previews(
    session: AsyncSession, org_id: UUID, conversation_id: UUID, owner_id: UUID, turn_id: UUID
) -> list[dict[str, Any]]:
    """The previews a turn shared, as its answer shows them."""
    rows = await session.execute(
        select(HermesPreview.id, HermesPreview.port)
        .where(
            HermesPreview.org_id == org_id,
            HermesPreview.conversation_id == conversation_id,
            HermesPreview.owner_id == owner_id,
            HermesPreview.turn_id == turn_id,
        )
        .order_by(HermesPreview.created_at, HermesPreview.id)
    )
    return [{"previewId": str(preview_id), "port": port} for preview_id, port in rows]
