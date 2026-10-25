"""hermes-egress per-connection checks and the organization's egress mode."""

import httpx
import pytest
from httpx import ASGITransport

from src.core.capability_catalog import Capability
from src.core.config import Settings, settings
from src.services import hermes_egress
from src.services.hermes_egress import set_organization_egress_mode

SECRET = "egress-service-secret-for-tests"


@pytest.fixture(autouse=True)
def _service_secret(monkeypatch) -> None:
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "HERMES_EGRESS_SERVICE_SECRET", SecretStr(SECRET))


async def _authorize(env, token: str, secret: str | None = SECRET) -> httpx.Response:  # noqa: ANN001
    headers = {"Authorization": f"Bearer {secret}"} if secret is not None else {}
    async with httpx.AsyncClient(transport=ASGITransport(app=env.app), base_url="http://test") as client:
        return await client.post(
            "/v1/internal/hermes/egress/authorize", json={"token": token}, headers=headers
        )


async def _set_mode(env, mode: str) -> None:  # noqa: ANN001
    async with env.sessions() as session:
        session.info["org_id"] = str(env.org_id)
        await set_organization_egress_mode(session, env.org_id, mode)  # type: ignore[arg-type]
        await session.commit()


async def test_a_bound_session_gets_its_mode_and_identity(hermes_env) -> None:
    opened = await hermes_env.open_session()

    response = await _authorize(hermes_env, opened["token"])

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "allow": True,
        "mode": "packages",
        "orgId": str(hermes_env.org_id),
        "sessionId": opened["sessionId"],
        "turnId": str(hermes_env.turn_id),
        "maxAgeSeconds": 5,
    }


@pytest.mark.parametrize("secret", [None, "wrong-secret-of-enough-length"])
async def test_only_the_egress_service_may_ask(hermes_env, secret) -> None:
    opened = await hermes_env.open_session()

    response = await _authorize(hermes_env, opened["token"], secret)

    assert response.status_code == 401


async def test_unconfigured_secret_refuses_everything(hermes_env, monkeypatch) -> None:
    from pydantic import SecretStr

    opened = await hermes_env.open_session()
    monkeypatch.setattr(settings, "HERMES_EGRESS_SERVICE_SECRET", SecretStr(""))

    response = await _authorize(hermes_env, opened["token"], "")

    assert response.status_code == 503


async def test_revoked_unbound_or_invalid_tokens_are_denied(hermes_env) -> None:
    opened = await hermes_env.open_session()
    await hermes_env.internal("DELETE", f"/sessions/{opened['sessionId']}/turn")
    unbound = (await _authorize(hermes_env, opened["token"])).json()
    await hermes_env.internal("POST", f"/sessions/{opened['sessionId']}/close")
    closed = (await _authorize(hermes_env, opened["token"])).json()
    garbage = (await _authorize(hermes_env, "not-a-token")).json()

    assert (unbound["allow"], unbound["reason"]) == (False, "hermes_turn_inactive")
    assert (closed["allow"], closed["reason"]) == (False, "hermes_session_revoked")
    assert (garbage["allow"], garbage["reason"]) == (False, "hermes_token_invalid")


async def test_capability_revocation_denies(hermes_env) -> None:
    opened = await hermes_env.open_session()
    await hermes_env.revoke(Capability.AGENTS_HERMES)

    body = (await _authorize(hermes_env, opened["token"])).json()

    assert (body["allow"], body["reason"]) == (False, "hermes_access_denied")


@pytest.mark.parametrize(
    ("maximum", "chosen", "web_access", "expected"),
    [
        ("public", "public", True, "public"),
        ("public", "public", False, "packages"),
        ("public", "none", True, "none"),
        ("public", "packages", True, "packages"),
    ],
)
async def test_effective_mode_follows_operator_organization_and_web_access(
    hermes_env, monkeypatch, maximum, chosen, web_access, expected
) -> None:
    monkeypatch.setattr(settings, "HERMES_EGRESS_MAX_MODE", maximum)
    await _set_mode(hermes_env, chosen)
    if web_access:
        await hermes_env.grant(Capability.WEB_ACCESS)
    opened = await hermes_env.open_session()

    body = (await _authorize(hermes_env, opened["token"])).json()

    assert body["mode"] == expected


async def test_lowering_the_operator_maximum_lowers_stored_modes(hermes_env, monkeypatch) -> None:
    monkeypatch.setattr(settings, "HERMES_EGRESS_MAX_MODE", "public")
    await _set_mode(hermes_env, "public")
    await hermes_env.grant(Capability.WEB_ACCESS)
    opened = await hermes_env.open_session()
    monkeypatch.setattr(settings, "HERMES_EGRESS_MAX_MODE", "none")

    body = (await _authorize(hermes_env, opened["token"])).json()

    assert body["mode"] == "none"


async def test_a_mode_above_the_maximum_is_refused(hermes_env, monkeypatch) -> None:
    monkeypatch.setattr(settings, "HERMES_EGRESS_MAX_MODE", "packages")

    with pytest.raises(hermes_egress.EgressModeAboveMaximum):
        await _set_mode(hermes_env, "public")


def test_the_business_api_needs_a_service_secret_when_egress_is_on(monkeypatch) -> None:
    from pydantic import SecretStr

    from src.services.hermes_egress import require_egress_secret

    monkeypatch.setattr(settings, "HERMES_ENABLED", True)
    monkeypatch.setattr(settings, "HERMES_EGRESS_SERVICE_SECRET", SecretStr("short"))
    with pytest.raises(RuntimeError, match="HERMES_EGRESS_SERVICE_SECRET"):
        require_egress_secret()
    monkeypatch.setattr(settings, "HERMES_EGRESS_MAX_MODE", "none")
    require_egress_secret()


def test_other_processes_load_settings_without_the_secret() -> None:
    Settings(SECRET_KEY="x" * 32, HERMES_ENABLED=True, HERMES_EGRESS_MAX_MODE="packages")


async def test_egress_checks_skip_rate_limiting_and_the_audit_journal() -> None:
    from starlette.requests import Request

    from src.middleware import audit, rate_limit

    calls: list[str] = []

    async def call_next(request: Request):  # noqa: ANN202
        from starlette.responses import Response

        calls.append(request.url.path)
        return Response()

    request = Request(
        {"type": "http", "method": "POST", "path": "/v1/internal/hermes/egress/authorize", "headers": [], "query_string": b""}
    )
    response = await rate_limit.RateLimitMiddleware(app=None).dispatch(request, call_next)  # type: ignore[arg-type]

    assert calls == ["/v1/internal/hermes/egress/authorize"]
    assert "X-RateLimit-Limit" not in response.headers
    assert request.url.path.startswith(audit._SKIPPED_PREFIXES)
