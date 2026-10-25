"""Admins choose the organization's Hermes egress mode within the operator maximum."""

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from src.core.auth import UserInfo
from src.core.authorization_dependencies import require_org_admin
from src.core.config import settings
from src.core.dependencies import get_current_org_id, get_org_db_session
from src.middleware.error_handler import setup_exception_handlers
from src.routers import admin_hermes


def _app(db, org_id) -> FastAPI:  # noqa: ANN001
    app = FastAPI()
    setup_exception_handlers(app)
    app.include_router(admin_hermes.router, prefix="/v1/admin")

    async def session():  # noqa: ANN202
        yield db

    app.dependency_overrides[require_org_admin] = lambda: UserInfo(user_id="u", org_id=str(org_id), role="org_admin")
    app.dependency_overrides[get_current_org_id] = lambda: org_id
    app.dependency_overrides[get_org_db_session] = session
    return app


async def test_default_mode_and_bounded_updates(db, test_org, monkeypatch) -> None:
    monkeypatch.setattr(settings, "HERMES_EGRESS_MAX_MODE", "packages")
    async with httpx.AsyncClient(transport=ASGITransport(app=_app(db, test_org.id)), base_url="http://t") as client:
        initial = (await client.get("/v1/admin/settings/hermes")).json()["data"]
        above = await client.put("/v1/admin/settings/hermes", json={"egressMode": "public"})
        lowered = (await client.put("/v1/admin/settings/hermes", json={"egressMode": "none"})).json()["data"]
        unknown = await client.put("/v1/admin/settings/hermes", json={"egressMode": "everything"})

    assert initial == {"enabled": False, "egressMode": "packages", "egressMaxMode": "packages"}
    assert above.status_code == 422
    assert lowered["egressMode"] == "none"
    assert unknown.status_code == 400
