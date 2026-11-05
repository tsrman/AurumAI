"""Settings of a Hermes-kind agent definition: model, internet mode, skills, knowledge bases."""

import uuid

import httpx

from tests.routers.test_agent_capabilities import PAYLOAD, _registry, client  # noqa: F401

HERMES = {**PAYLOAD, "kind": "hermes"}


async def test_a_new_hermes_agent_follows_the_organization(client: httpx.AsyncClient) -> None:  # noqa: F811
    created = await client.post("/v1/agent-definitions", json=HERMES)

    data = created.json()["data"]
    assert created.status_code == 201
    assert (data["hermesEgressMode"], data["useOrgHermesModel"], data["hermesSkillsScope"]) == (None, True, "all")


async def test_a_hermes_agent_stores_its_settings(client: httpx.AsyncClient) -> None:  # noqa: F811
    created = await client.post(
        "/v1/agent-definitions",
        json={**HERMES, "hermesEgressMode": "none", "useOrgHermesModel": False, "hermesSkillsScope": "selected"},
    )

    data = created.json()["data"]
    assert created.status_code == 201
    assert (data["hermesEgressMode"], data["useOrgHermesModel"], data["hermesSkillsScope"]) == (
        "none",
        False,
        "selected",
    )


async def test_the_internet_mode_can_be_lowered_and_cleared(client: httpx.AsyncClient) -> None:  # noqa: F811
    agent_id = (await client.post("/v1/agent-definitions", json=HERMES)).json()["data"]["id"]

    lowered = await client.patch(f"/v1/agent-definitions/{agent_id}", json={"hermesEgressMode": "packages"})
    cleared = await client.patch(f"/v1/agent-definitions/{agent_id}", json={"hermesEgressMode": None})

    assert lowered.json()["data"]["hermesEgressMode"] == "packages"
    assert cleared.json()["data"]["hermesEgressMode"] is None


async def test_an_unknown_internet_mode_is_refused(client: httpx.AsyncClient) -> None:  # noqa: F811
    refused = await client.post("/v1/agent-definitions", json={**HERMES, "hermesEgressMode": "everything"})

    assert refused.status_code == 422


async def test_a_simple_agent_takes_no_hermes_settings(client: httpx.AsyncClient) -> None:  # noqa: F811
    created = await client.post("/v1/agent-definitions", json={**PAYLOAD, "hermesEgressMode": "none"})
    agent_id = (await client.post("/v1/agent-definitions", json={**PAYLOAD, "name": "Plain"})).json()["data"]["id"]
    patched = await client.patch(f"/v1/agent-definitions/{agent_id}", json={"useOrgHermesModel": False})

    assert created.status_code == 422
    assert patched.status_code == 422


async def test_a_hermes_agent_may_name_skills_and_knowledge_bases(client: httpx.AsyncClient) -> None:  # noqa: F811
    refused = await client.post(
        "/v1/agent-definitions", json={**HERMES, "skillIds": [str(uuid.uuid4())]}
    )

    # Unknown dependencies are refused like for any agent, not because Hermes takes none.
    assert refused.status_code in (403, 404, 409, 422)
    assert "takes no skills" not in refused.text
