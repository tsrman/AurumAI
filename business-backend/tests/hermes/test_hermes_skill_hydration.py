"""Code bundles are limited by the live agent bound to the runner's turn."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, update

from src.models.access_grant import AccessGrant
from src.models.agent_definition import AgentDefinition
from src.models.interaction import InteractionExecution
from tests.hermes.test_hermes_agent_settings import _agent
from tests.hermes.test_hermes_mcp_skills import _skill


async def _code_skill(env, **values):  # noqa: ANN001, ANN003, ANN202
    return await _skill(
        env,
        kind="code",
        bundle_storage_key="skills/test-bundle.zip",
        bundle_checksum="a" * 64,
        **values,
    )


@pytest.mark.parametrize("chat", [True, False])
@pytest.mark.parametrize("scope", ["selected", "empty", "all", "general"])
async def test_hydration_uses_the_bound_agents_skill_scope(hermes_env, chat, scope) -> None:  # noqa: ANN001
    mine = await _code_skill(hermes_env)
    other = await _code_skill(hermes_env)
    await _code_skill(hermes_env, granted=False)
    await _code_skill(hermes_env, owned=True)
    if scope != "general":
        await _agent(
            hermes_env,
            chat=chat,
            hermes_skills_scope="all" if scope == "all" else "selected",
            skill_ids=[] if scope == "empty" else [mine],
        )
    await hermes_env.open_session()
    try:
        response = await hermes_env.internal("GET", "/platform-skills")

        assert response.status_code == 200, response.text
        expected = set() if scope == "empty" else {str(mine)}
        if scope in {"all", "general"}:
            expected.add(str(other))
        assert {item["skillId"] for item in response.json()["skills"]} == expected
    finally:
        async with hermes_env.sessions() as db:
            await db.execute(
                update(InteractionExecution)
                .where(InteractionExecution.id == hermes_env.execution_id)
                .values(agent_definition_id=None)
            )
            await db.commit()


async def test_hydration_rechecks_selected_skill_grants(hermes_env) -> None:
    mine = await _code_skill(hermes_env)
    await _code_skill(hermes_env)
    await _agent(hermes_env, hermes_skills_scope="selected", skill_ids=[mine])
    await hermes_env.open_session()

    before = await hermes_env.internal("GET", "/platform-skills")
    async with hermes_env.sessions() as db:
        await db.execute(
            delete(AccessGrant).where(
                AccessGrant.org_id == hermes_env.org_id,
                AccessGrant.resource_type == "skill",
                AccessGrant.resource_id == str(mine),
            )
        )
        await db.commit()
    after = await hermes_env.internal("GET", "/platform-skills")

    assert [item["skillId"] for item in before.json()["skills"]] == [str(mine)]
    assert after.status_code == 200 and after.json() == {"skills": []}


async def test_hydration_stops_when_the_bound_agent_is_archived(hermes_env) -> None:
    mine = await _code_skill(hermes_env)
    agent_id = await _agent(hermes_env, hermes_skills_scope="selected", skill_ids=[mine])
    await hermes_env.open_session()
    before = await hermes_env.internal("GET", "/platform-skills")
    async with hermes_env.sessions() as db:
        await db.execute(
            update(AgentDefinition)
            .where(AgentDefinition.id == agent_id, AgentDefinition.org_id == hermes_env.org_id)
            .values(archived_at=datetime.now(UTC))
        )
        await db.commit()

    after = await hermes_env.internal("GET", "/platform-skills")

    assert before.status_code == 200
    assert after.status_code == 403 and "hermes_agent_unavailable" in after.text


@pytest.mark.parametrize("chat", [True, False])
async def test_hydration_stops_when_the_bound_agent_grant_is_revoked(hermes_env, chat) -> None:  # noqa: ANN001
    mine = await _code_skill(hermes_env)
    agent_id = await _agent(hermes_env, chat=chat, hermes_skills_scope="selected", skill_ids=[mine])
    await hermes_env.open_session()
    try:
        before = await hermes_env.internal("GET", "/platform-skills")
        async with hermes_env.sessions() as db:
            await db.execute(
                delete(AccessGrant).where(
                    AccessGrant.org_id == hermes_env.org_id,
                    AccessGrant.resource_type == "agent_definition",
                    AccessGrant.resource_id == str(agent_id),
                )
            )
            await db.commit()
        after = await hermes_env.internal("GET", "/platform-skills")

        assert before.status_code == 200
        assert after.status_code == 403 and "hermes_agent_unavailable" in after.text
    finally:
        async with hermes_env.sessions() as db:
            await db.execute(
                update(InteractionExecution)
                .where(InteractionExecution.id == hermes_env.execution_id)
                .values(agent_definition_id=None)
            )
            await db.commit()


@pytest.mark.parametrize("opened", [False, True])
async def test_hydration_requires_a_session_bound_to_the_active_turn(hermes_env, opened) -> None:  # noqa: ANN001
    await _code_skill(hermes_env)
    if opened:
        await hermes_env.open_session(bind=False)

    response = await hermes_env.internal("GET", "/platform-skills")

    assert response.status_code == 403 and "hermes_turn_inactive" in response.text
