"""Admins choose the organization's Hermes egress mode within the operator maximum
and the model Hermes runs on among the models the organization grants."""

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

    assert initial == {"enabled": False, "egressMode": "packages", "egressMaxMode": "packages", "model": None}
    assert above.status_code == 422
    assert lowered["egressMode"] == "none"
    assert unknown.status_code == 400


async def test_the_model_must_be_granted_and_can_be_cleared(db, test_org, monkeypatch) -> None:
    from fastapi import HTTPException

    checked: list[str] = []

    async def allowed(session, org_id, model, **_):  # noqa: ANN001, ANN003, ANN202
        checked.append(model)
        if model != "granted-model":
            raise HTTPException(status_code=403, detail="Model not allowed")

    monkeypatch.setattr(admin_hermes, "ensure_model_allowed", allowed)
    async with httpx.AsyncClient(transport=ASGITransport(app=_app(db, test_org.id)), base_url="http://t") as client:
        refused = await client.put("/v1/admin/settings/hermes", json={"model": "other"})
        chosen = (await client.put("/v1/admin/settings/hermes", json={"model": "granted-model"})).json()["data"]
        mode_only = (await client.put("/v1/admin/settings/hermes", json={"egressMode": "none"})).json()["data"]
        cleared = (await client.put("/v1/admin/settings/hermes", json={"model": None})).json()["data"]

    assert refused.status_code == 403
    assert chosen["model"] == "granted-model"
    assert mode_only["model"] == "granted-model" and mode_only["egressMode"] == "none"
    assert cleared["model"] is None
    assert checked == ["other", "granted-model"]
