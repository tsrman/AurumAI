"""Hermes session tokens: issuance, audience separation and rate-limit identity."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from httpx import ASGITransport
from jwt.exceptions import PyJWTError
from starlette.requests import Request

from src.core import auth
from src.core.auth import UserInfo, get_current_user_or_session
from src.core.capability_catalog import Capability
from src.core.config import settings
from src.middleware.rate_limit import _rate_limit_identity
from src.services.hermes_session_token import (
    HERMES_SESSION_AUDIENCE,
    InvalidHermesSessionToken,
    hermes_session_token_service,
)
from src.services.session_token import session_token_service


def _issued(now: datetime | None = None):  # noqa: ANN202
    return hermes_session_token_service.issue(
        org_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        generation=3,
        session_id=uuid.uuid4(),
        token_version=2,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        now=now,
    )


def test_issued_token_carries_identity_only_with_its_own_audience() -> None:
    issued = _issued(now=datetime.now(timezone.utc) - timedelta(minutes=1))
    claims = jwt.decode(
        issued.token, settings.SECRET_KEY, algorithms=["HS256"], audience=HERMES_SESSION_AUDIENCE
    )

    assert claims["type"] == "hermes_session"
    assert (claims["generation"], claims["tv"]) == (3, 2)
    assert claims["sid"] == str(issued.session_id)
    assert claims["exp"] == int(issued.expires_at.timestamp())
    assert "permissions" not in claims and "turn_id" not in claims


def test_runner_session_decoder_rejects_hermes_tokens() -> None:
    with pytest.raises(PyJWTError):
        session_token_service.decode(_issued().token)


def test_hermes_decoder_rejects_runner_tokens() -> None:
    runner = session_token_service.issue(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))

    with pytest.raises(InvalidHermesSessionToken):
        hermes_session_token_service.decode(runner)


@pytest.mark.parametrize(
    "change",
    [
        {"type": "session"},
        {"permissions": ["x"]},
        {"generation": 0},
        {"tv": True},
        {"sid": "not-a-uuid"},
    ],
)
def test_hermes_decoder_rejects_tampered_claims(change: dict[str, object]) -> None:
    claims = jwt.decode(
        _issued().token, settings.SECRET_KEY, algorithms=["HS256"], audience=HERMES_SESSION_AUDIENCE
    )
    forged = jwt.encode({**claims, **change}, settings.SECRET_KEY, algorithm="HS256")

    with pytest.raises(InvalidHermesSessionToken):
        hermes_session_token_service.decode(forged)


async def test_session_routes_reject_hermes_tokens(monkeypatch) -> None:
    async def reject(*_args: object, **_kwargs: object) -> UserInfo:
        raise HTTPException(status_code=401, detail="Invalid token")

    monkeypatch.setattr(auth, "get_current_user", reject)
    request = Request(
        {"type": "http", "method": "POST", "path": "/internal", "headers": [], "query_string": b"", "path_params": {}}
    )
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=_issued().token)

    with pytest.raises(HTTPException) as exc_info:
        await get_current_user_or_session(request, credentials)

    assert exc_info.value.status_code == 401


async def test_rate_limit_counts_a_hermes_sandbox_against_its_user() -> None:
    org_id, user_id = uuid.uuid4(), uuid.uuid4()
    token = hermes_session_token_service.issue(
        org_id=org_id,
        user_id=user_id,
        conversation_id=uuid.uuid4(),
        generation=1,
        session_id=uuid.uuid4(),
        token_version=1,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    ).token
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/hermes/chat/completions",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "client": ("127.0.0.1", 1),
        }
    )

    with patch(
        "src.middleware.rate_limit.keycloak_client.decode_token",
        new_callable=AsyncMock,
        side_effect=PyJWTError("not a keycloak token"),
    ):
        assert await _rate_limit_identity(request) == (str(org_id), str(user_id))


async def test_runner_opens_a_session_for_its_conversation(hermes_env) -> None:
    body = await hermes_env.open_session(bind=False)

    claims = hermes_session_token_service.decode(body["token"])
    assert claims["conversation_id"] == str(hermes_env.conversation_id)
    assert claims["sub"] == str(hermes_env.user_id)
    assert claims["sid"] == body["sessionId"]
    assert (claims["generation"], claims["tv"]) == (1, 1)


async def test_a_hermes_token_cannot_manage_sessions(hermes_env) -> None:
    body = await hermes_env.open_session(bind=False)
    async with httpx.AsyncClient(transport=ASGITransport(app=hermes_env.app), base_url="http://test") as client:
        response = await client.post(
            "/v1/internal/hermes/sessions", headers={"Authorization": f"Bearer {body['token']}"}
        )

    assert response.status_code == 401


async def test_opening_needs_the_capability_but_closing_never_does(hermes_env) -> None:
    body = await hermes_env.open_session(bind=False)
    await hermes_env.revoke(Capability.AGENTS_HERMES)

    denied = await hermes_env.internal("POST", "/sessions")
    closed = await hermes_env.internal("POST", f"/sessions/{body['sessionId']}/close")
    again = await hermes_env.internal("POST", f"/sessions/{body['sessionId']}/close")

    assert denied.status_code == 403 and "hermes_access_denied" in denied.text
    assert (closed.status_code, again.status_code) == (204, 204)


async def test_binding_needs_the_conversations_active_turn(hermes_env) -> None:
    body = await hermes_env.open_session(bind=False)
    path = f"/sessions/{body['sessionId']}/turn"

    other = await hermes_env.internal("PUT", path, json=hermes_env.turn_body(turnId=str(uuid.uuid4())))
    await hermes_env.finish_turn()
    finished = await hermes_env.internal("PUT", path, json=hermes_env.turn_body())

    assert other.status_code == 403 and "hermes_turn_inactive" in other.text
    assert finished.status_code == 403 and "hermes_turn_inactive" in finished.text


async def test_binding_a_closed_or_unknown_session_fails(hermes_env) -> None:
    body = await hermes_env.open_session(bind=False)
    await hermes_env.internal("POST", f"/sessions/{body['sessionId']}/close")
    turn = hermes_env.turn_body()

    closed = await hermes_env.internal("PUT", f"/sessions/{body['sessionId']}/turn", json=turn)
    unknown = await hermes_env.internal("PUT", f"/sessions/{uuid.uuid4()}/turn", json=turn)

    assert closed.status_code == 409
    assert unknown.status_code == 404


@pytest.mark.parametrize(
    "case", ["unknown_run", "node_without_key", "supervisor_with_key", "finished_run", "phase_without_run"]
)
async def test_workflow_correlation_must_match_the_turns_live_run(hermes_env, case: str) -> None:
    run_id = await hermes_env.add_workflow_run(status="completed" if case == "finished_run" else "running")
    correlation = {
        "unknown_run": {"workflowRunId": str(uuid.uuid4()), "workflowPhase": "supervisor_prepare"},
        "node_without_key": {"workflowRunId": str(run_id), "workflowPhase": "node"},
        "supervisor_with_key": {
            "workflowRunId": str(run_id),
            "workflowPhase": "supervisor_prepare",
            "workflowNodeKey": "a",
        },
        "finished_run": {"workflowRunId": str(run_id), "workflowPhase": "supervisor_prepare"},
        "phase_without_run": {"workflowPhase": "supervisor_prepare"},
    }[case]
    body = await hermes_env.open_session(bind=False)

    response = await hermes_env.internal(
        "PUT",
        f"/sessions/{body['sessionId']}/turn",
        json=hermes_env.turn_body(**correlation),
    )

    assert response.status_code == 422
    assert "hermes_correlation_invalid" in response.text


async def test_switching_hermes_off_stops_opening_but_not_revocation(
    hermes_env, monkeypatch
) -> None:
    body = await hermes_env.open_session(bind=False)
    monkeypatch.setattr(settings, "HERMES_ENABLED", False)
    path = f"/sessions/{body['sessionId']}"

    opened = await hermes_env.internal("POST", "/sessions")
    bound = await hermes_env.internal("PUT", f"{path}/turn", json=hermes_env.turn_body())
    unbound = await hermes_env.internal("DELETE", f"{path}/turn")
    closed = await hermes_env.internal("POST", f"{path}/close")

    assert (opened.status_code, bound.status_code) == (404, 404)
    assert (unbound.status_code, closed.status_code) == (204, 204)


@pytest.mark.parametrize("case", ["unknown", "missing", "workflow_step_mismatch", "ordinary_in_workflow"])
async def test_the_bound_execution_must_act_in_the_bound_step(hermes_env, case: str) -> None:
    run_id = await hermes_env.add_workflow_run()
    node = {"workflowRunId": str(run_id), "workflowPhase": "node", "workflowNodeKey": "research"}
    other_node = await hermes_env.add_execution("node", "summary")
    body = {
        "unknown": hermes_env.turn_body(executionId=str(uuid.uuid4())),
        "missing": {"turnId": str(hermes_env.turn_id)},
        "workflow_step_mismatch": hermes_env.turn_body(**node, executionId=str(other_node)),
        "ordinary_in_workflow": hermes_env.turn_body(**node),
    }[case]
    opened = await hermes_env.open_session(bind=False)

    response = await hermes_env.internal("PUT", f"/sessions/{opened['sessionId']}/turn", json=body)

    if case == "missing":
        assert response.status_code == 400
    else:
        assert response.status_code == 422
        assert "hermes_correlation_invalid" in response.text


async def test_unbinding_and_closing_clear_the_execution(hermes_env) -> None:
    from sqlalchemy import select

    from src.models.hermes_session import HermesSession

    opened = await hermes_env.open_session()
    path = f"/sessions/{opened['sessionId']}"

    async def execution() -> object:
        async with hermes_env.sessions() as db:
            return await db.scalar(
                select(HermesSession.active_execution_id).where(
                    HermesSession.id == uuid.UUID(opened["sessionId"])
                )
            )

    bound = await execution()
    await hermes_env.internal("DELETE", f"{path}/turn")
    unbound = await execution()
    await hermes_env.internal("PUT", f"{path}/turn", json=hermes_env.turn_body())
    await hermes_env.internal("POST", f"{path}/close")

    assert bound == hermes_env.execution_id
    assert unbound is None
    assert await execution() is None


async def test_the_current_session_is_read_without_rotating_its_token(hermes_env) -> None:
    from tests.hermes.test_hermes_mcp import _rpc

    missing = await hermes_env.internal("GET", "/sessions/current")
    opened = await hermes_env.open_session(bind=False)

    current = await hermes_env.internal("GET", "/sessions/current")
    still_valid = await _rpc(hermes_env, opened["token"], {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    await hermes_env.expire_session(opened["sessionId"])
    expired = await hermes_env.internal("GET", "/sessions/current")

    assert missing.status_code == 404 and "hermes_session_not_found" in missing.text
    assert current.status_code == 200 and current.json()["sessionId"] == opened["sessionId"]
    assert still_valid.status_code == 200
    assert expired.status_code == 404


async def test_touching_keeps_the_runner_of_the_active_turn_only(hermes_env, monkeypatch) -> None:
    from src.routers import hermes_internal

    touched: list[tuple[object, ...]] = []

    async def get_or_create(conversation_id, user_id, org_id, generation=1):  # noqa: ANN001, ANN202
        touched.append((conversation_id, user_id, org_id, generation))

    monkeypatch.setattr(hermes_internal.session_manager, "get_or_create", get_or_create)

    active = await hermes_env.internal("POST", f"/turns/{hermes_env.turn_id}/touch")
    other = await hermes_env.internal("POST", f"/turns/{uuid.uuid4()}/touch")
    await hermes_env.finish_turn()
    finished = await hermes_env.internal("POST", f"/turns/{hermes_env.turn_id}/touch")

    assert active.status_code == 204
    assert touched == [(str(hermes_env.conversation_id), str(hermes_env.user_id), str(hermes_env.org_id), 1)]
    assert other.status_code == 403 and finished.status_code == 403


async def test_a_turn_reads_its_users_memory_and_stores_it_on_the_version_it_read(hermes_env) -> None:
    empty = await hermes_env.internal("GET", "/memory")
    stored = await hermes_env.internal("PUT", "/memory", json={"kind": "user", "content": "likes tea", "baseVersion": 0})
    stale = await hermes_env.internal("PUT", "/memory", json={"kind": "user", "content": "older read", "baseVersion": 0})
    read = await hermes_env.internal("GET", "/memory")
    await hermes_env.finish_turn()
    after_turn = await hermes_env.internal("PUT", "/memory", json={"kind": "user", "content": "late", "baseVersion": 1})

    assert empty.json() == {"memory": {"content": "", "version": 0}, "user": {"content": "", "version": 0}}
    assert stored.status_code == 200 and stored.json() == {"content": "likes tea", "version": 1}
    assert stale.status_code == 409 and "hermes_memory_conflict" in stale.text
    assert read.json()["user"] == {"content": "likes tea", "version": 1}
    assert after_turn.status_code == 403 and "hermes_turn_inactive" in after_turn.text


async def test_a_turn_drafts_the_skills_its_sandbox_holds_for_its_user(hermes_env, monkeypatch) -> None:
    from sqlalchemy import select, text

    from src.models.hermes_skill_draft import HermesSkillDraft
    from src.services import hermes_skill_drafts

    async def put_bytes(key: str, data: bytes, **_: object) -> None:
        return None

    monkeypatch.setattr(hermes_skill_drafts.object_storage, "put_bytes", put_bytes)
    skill = {
        "path": "notes/standup",
        "files": [{"path": "SKILL.md", "content": "---\nname: standup\ndescription: Write the standup\n---\nSteps.\n"}],
    }
    drafted = await hermes_env.internal("PUT", "/skill-drafts", json=skill)
    again = await hermes_env.internal("PUT", "/skill-drafts", json=skill)
    invalid = await hermes_env.internal(
        "PUT", "/skill-drafts", json={"path": "notes/x", "files": [{"path": "SKILL.md", "content": "no frontmatter"}]}
    )
    await hermes_env.finish_turn()
    after_turn = await hermes_env.internal("PUT", "/skill-drafts", json={**skill, "path": "notes/other"})
    async with hermes_env.sessions() as check:
        await check.execute(text("SELECT set_config('app.current_org_id', :org, false)"), {"org": str(hermes_env.org_id)})
        owners = (await check.execute(select(HermesSkillDraft.user_id))).scalars().all()

    assert drafted.json() == {"outcome": "drafted"} and again.json() == {"outcome": "unchanged"}
    assert invalid.status_code == 422 and "hermes_skill_invalid" in invalid.text
    assert after_turn.status_code == 403 and "hermes_turn_inactive" in after_turn.text
    assert owners == [hermes_env.user_id]
