"""Previews of the apps Hermes runs in its sandbox: shared over the bridge,
opened by the owner with a one-time ticket at the preview's own origin, and
checked on every connection by the preview service."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from httpx import ASGITransport
from sqlalchemy import select, update

from src.core.auth import UserInfo, get_current_user
from src.core.config import settings
from src.core.dependencies import get_org_db_session
from src.models.hermes_preview import HermesPreview, HermesPreviewTicket
from src.models.hermes_session import HermesSession
from src.routers import hermes_previews as routes
from tests.hermes.test_hermes_mcp import _call, _rpc, _tool_payload

SECRET = "preview-service-secret"


@pytest.fixture
def previews_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "HERMES_PREVIEW_ENABLED", True)
    monkeypatch.setattr(settings, "HERMES_PREVIEW_ORIGIN", "http://{preview}.preview.localhost:8090")
    monkeypatch.setattr(settings, "HERMES_PREVIEW_SERVICE_SECRET", SecretStr(SECRET))


def _app(env: Any, user_id: uuid.UUID | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(routes.router, prefix="/v1")
    app.include_router(routes.internal_router, prefix="/v1/internal")

    async def session():  # noqa: ANN202
        async with env.sessions() as db:
            db.info["org_id"] = str(env.org_id)
            yield db

    app.dependency_overrides[get_current_user] = lambda: UserInfo(
        user_id=str(user_id or env.user_id), org_id=str(env.org_id), role="member"
    )
    app.dependency_overrides[get_org_db_session] = session
    return app


async def _post(app: FastAPI, path: str, body: dict[str, Any] | None = None, secret: str = SECRET) -> httpx.Response:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.post(path, json=body, headers={"Authorization": f"Bearer {secret}"})


async def _share(env: Any, port: int = 8899, request_id: int = 7) -> tuple[dict[str, Any], dict[str, Any]]:
    opened = await env.open_session()
    is_error, payload = _tool_payload(await _rpc(env, opened["token"], _call("share_preview", {"port": port}, request_id)))
    assert not is_error, payload
    return opened, payload


async def _ticket(env: Any, preview_id: str) -> tuple[str, str]:
    response = await _post(_app(env), f"/v1/conversations/{env.conversation_id}/previews/{preview_id}/ticket")
    assert response.status_code == 200, response.text
    url = urlsplit(response.json()["url"])
    return url.netloc.split(".", 1)[0], parse_qs(url.query)["ticket"][0]


async def test_a_shared_port_opens_once_for_its_owner_at_its_own_origin(hermes_env, previews_on) -> None:
    opened, shared = await _share(hermes_env)
    label, ticket = await _ticket(hermes_env, shared["previewId"])
    app = _app(hermes_env)

    redeemed = (await _post(app, "/v1/internal/hermes/previews/redeem", {"preview": label, "ticket": ticket})).json()
    again = (await _post(app, "/v1/internal/hermes/previews/redeem", {"preview": label, "ticket": ticket})).json()
    checked = (
        await _post(app, "/v1/internal/hermes/previews/check", {"preview": label, "grant": redeemed["grant"]})
    ).json()

    assert label == uuid.UUID(shared["previewId"]).hex and shared["port"] == 8899
    assert redeemed["allow"] and redeemed["port"] == 8899
    assert redeemed["sessionId"] == uuid.UUID(opened["sessionId"]).hex
    # A ticket opens once.
    assert again == {"allow": False, "reason": "hermes_preview_denied", "maxAgeSeconds": 5}
    assert checked["allow"] and checked["port"] == 8899 and "grant" not in checked


async def test_sharing_a_port_again_keeps_one_preview(hermes_env, previews_on) -> None:
    opened, first = await _share(hermes_env, request_id=1)
    _, again = _tool_payload(await _rpc(hermes_env, opened["token"], _call("share_preview", {"port": 8899}, 2)))
    _, low = _tool_payload(await _rpc(hermes_env, opened["token"], _call("share_preview", {"port": 80}, 3)))

    async with hermes_env.sessions() as db:
        rows = (await db.scalars(select(HermesPreview).where(HermesPreview.org_id == hermes_env.org_id))).all()
    assert again["previewId"] == first["previewId"] and len(rows) == 1
    assert low == {"error": "invalid_arguments"}


async def test_strangers_forged_and_expired_tickets_and_other_previews_are_refused(hermes_env, previews_on) -> None:
    _, shared = await _share(hermes_env)
    _, other = await _share(hermes_env, port=9000, request_id=8)
    stranger = await _post(
        _app(hermes_env, uuid.uuid4()), f"/v1/conversations/{hermes_env.conversation_id}/previews/{shared['previewId']}/ticket"
    )
    label, ticket = await _ticket(hermes_env, shared["previewId"])
    other_label = uuid.UUID(other["previewId"]).hex
    app = _app(hermes_env)

    elsewhere = (await _post(app, "/v1/internal/hermes/previews/redeem", {"preview": other_label, "ticket": ticket})).json()
    forged = (await _post(app, "/v1/internal/hermes/previews/redeem", {"preview": label, "ticket": ticket[:-4] + "AAAA"})).json()
    no_secret = await _post(app, "/v1/internal/hermes/previews/redeem", {"preview": label, "ticket": ticket}, secret="nope")
    async with hermes_env.sessions() as db:
        await db.execute(
            update(HermesPreviewTicket)
            .where(HermesPreviewTicket.org_id == hermes_env.org_id)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        await db.commit()
    expired = (await _post(app, "/v1/internal/hermes/previews/redeem", {"preview": label, "ticket": ticket})).json()

    assert stranger.status_code == 404
    assert not elsewhere["allow"] and not forged["allow"] and not expired["allow"]
    assert no_secret.status_code == 401


async def test_a_grant_opens_only_its_preview_and_only_while_the_session_is_open(hermes_env, previews_on) -> None:
    from src.core.capability_catalog import Capability

    _, shared = await _share(hermes_env)
    _, other = await _share(hermes_env, port=9000, request_id=8)
    label, ticket = await _ticket(hermes_env, shared["previewId"])
    app = _app(hermes_env)
    grant = (await _post(app, "/v1/internal/hermes/previews/redeem", {"preview": label, "ticket": ticket})).json()["grant"]

    elsewhere = (
        await _post(app, "/v1/internal/hermes/previews/check", {"preview": uuid.UUID(other["previewId"]).hex, "grant": grant})
    ).json()
    await hermes_env.revoke(Capability.AGENTS_HERMES)
    revoked = (await _post(app, "/v1/internal/hermes/previews/check", {"preview": label, "grant": grant})).json()
    await hermes_env.grant(Capability.AGENTS_HERMES)
    async with hermes_env.sessions() as db:
        [session_id] = (await db.scalars(select(HermesSession.id).where(HermesSession.org_id == hermes_env.org_id))).all()
    await hermes_env.expire_session(str(session_id))
    closed = (await _post(app, "/v1/internal/hermes/previews/check", {"preview": label, "grant": grant})).json()
    closed_ticket = await _post(app, f"/v1/conversations/{hermes_env.conversation_id}/previews/{shared['previewId']}/ticket")

    assert not elsewhere["allow"] and elsewhere["reason"] == "hermes_preview_denied"
    assert not revoked["allow"]
    assert closed == {"allow": False, "reason": "hermes_preview_closed", "maxAgeSeconds": 5}
    assert closed_ticket.status_code == 404 and closed_ticket.json()["detail"]["code"] == "hermes_preview_closed"


async def test_previews_are_off_until_the_operator_gives_an_origin(hermes_env, monkeypatch) -> None:
    monkeypatch.setattr(settings, "HERMES_PREVIEW_ENABLED", True)
    monkeypatch.setattr(settings, "HERMES_PREVIEW_ORIGIN", "")
    opened = await hermes_env.open_session(bind=False)

    listed = await _rpc(hermes_env, opened["token"], {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert "share_preview" not in [tool["name"] for tool in listed.json()["result"]["tools"]]


async def test_the_answer_of_the_turn_lists_its_previews(hermes_env, previews_on) -> None:
    from src.services.hermes_previews import turn_previews

    _, shared = await _share(hermes_env)
    env = hermes_env
    async with env.sessions() as db:
        listed = await turn_previews(db, env.org_id, env.conversation_id, env.user_id, env.turn_id)
        stranger = await turn_previews(db, env.org_id, env.conversation_id, uuid.uuid4(), env.turn_id)

    assert listed == [{"previewId": shared["previewId"], "port": 8899}] and stranger == []
