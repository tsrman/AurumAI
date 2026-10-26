"""Platform skills over the MCP bridge: a live listing without secrets, and runs
as tool-worker workflows whose id makes a repeated request attach."""

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from src.core.config import settings
from src.models.access_grant import AccessGrant
from src.models.skill import Skill
from src.services import hermes_mcp, hermes_skills
from src.services.session_token import session_token_service
from tests.hermes.test_hermes_mcp import _call, _rpc, _tool_payload


async def _skill(env, *, kind: str = "mcp", granted: bool = True, owned: bool = False, **values: Any) -> uuid.UUID:  # noqa: ANN001
    skill_id = uuid.uuid4()
    async with env.sessions() as db:
        db.add(
            Skill(
                id=skill_id,
                org_id=env.org_id,
                name=values.pop("name", f"skill-{skill_id.hex[:6]}"),
                type=kind,
                owning_conversation_id=env.conversation_id if owned else None,
                mcp_transport="http",
                mcp_endpoint="https://mcp.internal.example/secret-path",
                mcp_auth_token="skill-secret-token",
                input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
                **values,
            )
        )
        await db.flush()
        if granted:
            db.add(
                AccessGrant(
                    id=uuid.uuid4(),
                    org_id=env.org_id,
                    resource_type="skill",
                    resource_id=str(skill_id),
                    principal_type="user",
                    principal_id=env.user_id,
                    permission="read",
                    created_via="provisioning",
                )
            )
        await db.commit()
    return skill_id


class FakeHandle:
    def __init__(self, result: Any, delay: float = 0) -> None:
        self._result, self._delay = result, delay

    async def result(self) -> Any:
        await asyncio.sleep(self._delay)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeTemporal:
    def __init__(self, result: Any = None, *, started: bool = False, delay: float = 0) -> None:
        self.starts: list[dict[str, Any]] = []
        self.attached: list[str] = []
        self.handle = FakeHandle({"output": {"ok": 1}} if result is None else result, delay)
        self.started = started

    async def start_workflow(self, name: str, call: dict[str, Any], **options: Any) -> FakeHandle:
        self.starts.append({"name": name, "call": call, **options})
        if self.started:
            raise WorkflowAlreadyStartedError(options["id"], name)
        return self.handle

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        self.attached.append(workflow_id)
        return self.handle


@pytest.fixture
def temporal(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    def install(fake: FakeTemporal) -> FakeTemporal:
        monkeypatch.setattr(hermes_skills, "get_temporal_client", lambda: fake)
        return fake

    return install


async def test_the_catalogue_lists_the_two_skill_tools(hermes_env) -> None:
    opened = await hermes_env.open_session(bind=False)

    listed = await _rpc(hermes_env, opened["token"], {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    names = [tool["name"] for tool in listed.json()["result"]["tools"]]
    assert names[-2:] == ["list_skills", "run_skill"]


async def test_listing_shows_granted_mcp_and_tool_skills_without_secrets(hermes_env) -> None:
    mcp = await _skill(hermes_env, name="weather")
    tool = await _skill(hermes_env, kind="tool", activity_name="execute_hybrid_search")
    for hidden in (
        {"kind": "code"},
        {"kind": "agent"},
        {"granted": False},
        {"owned": True},
    ):
        await _skill(hermes_env, **hidden)
    opened = await hermes_env.open_session()

    response = await _rpc(hermes_env, opened["token"], _call("list_skills", {}))

    is_error, payload = _tool_payload(response)
    assert not is_error
    assert {item["skillId"] for item in payload["skills"]} == {str(mcp), str(tool)}
    weather = next(item for item in payload["skills"] if item["skillId"] == str(mcp))
    assert set(weather) == {"skillId", "name", "description", "inputSchema"}
    assert weather["inputSchema"]["properties"] == {"q": {"type": "string"}}
    assert "secret" not in response.text


async def test_a_run_starts_one_tool_worker_workflow_for_the_call(hermes_env, temporal) -> None:
    skill_id = await _skill(hermes_env)
    fake = temporal(FakeTemporal())
    opened = await hermes_env.open_session()
    arguments = {"skill_id": str(skill_id), "arguments": {"q": "x"}}

    result = _tool_payload(await _rpc(hermes_env, opened["token"], _call("run_skill", arguments)))

    assert result == (False, {"skillId": str(skill_id), "output": '{"ok": 1}', "truncated": False})
    [start] = fake.starts
    invocation = hermes_mcp.invocation_id(uuid.UUID(opened["sessionId"]), 7, "run_skill", arguments)
    assert start["name"] == "HermesSkillCallWorkflow"
    assert start["id"] == f"hermes-skill-{invocation}"
    assert start["task_queue"] == "tools"
    call = start["call"]
    assert (call["skill_id"], call["conversation_id"], call["arguments"]) == (
        str(skill_id),
        str(hermes_env.conversation_id),
        {"q": "x"},
    )
    # A runner token for this conversation, so tool-worker fetches the skill
    # under the user's live grant, as for the runner's own calls.
    claims = session_token_service.decode(call["session_token"])
    assert claims["type"] == "session"
    assert (claims["conversation_id"], claims["sub"], claims["generation"]) == (
        str(hermes_env.conversation_id),
        str(hermes_env.user_id),
        1,
    )
    assert opened["token"] not in json.dumps(call)


async def test_a_repeated_call_attaches_to_its_run(hermes_env, temporal) -> None:
    skill_id = await _skill(hermes_env)
    fake = temporal(FakeTemporal(started=True))
    opened = await hermes_env.open_session()

    result = _tool_payload(await _rpc(hermes_env, opened["token"], _call("run_skill", {"skill_id": str(skill_id)})))

    assert not result[0]
    assert fake.attached == [fake.starts[0]["id"]]


@pytest.mark.parametrize("case", ["ungranted", "code", "owned", "unknown"])
async def test_only_runnable_granted_skills_run(hermes_env, temporal, case: str) -> None:
    skill_id = (
        uuid.uuid4()
        if case == "unknown"
        else await _skill(
            hermes_env,
            kind="code" if case == "code" else "mcp",
            granted=case != "ungranted",
            owned=case == "owned",
        )
    )
    fake = temporal(FakeTemporal())
    opened = await hermes_env.open_session()

    result = _tool_payload(await _rpc(hermes_env, opened["token"], _call("run_skill", {"skill_id": str(skill_id)})))

    assert result == (True, {"error": "skill_not_found"})
    assert fake.starts == []


async def test_a_revoked_grant_stops_the_next_run(hermes_env, temporal) -> None:
    from sqlalchemy import delete

    skill_id = await _skill(hermes_env)
    fake = temporal(FakeTemporal())
    opened = await hermes_env.open_session()
    first = _tool_payload(await _rpc(hermes_env, opened["token"], _call("run_skill", {"skill_id": str(skill_id)})))
    async with hermes_env.sessions() as db:
        await db.execute(delete(AccessGrant).where(AccessGrant.resource_id == str(skill_id)))
        await db.commit()

    second = _tool_payload(
        await _rpc(hermes_env, opened["token"], _call("run_skill", {"skill_id": str(skill_id)}, 8))
    )

    assert not first[0] and second == (True, {"error": "skill_not_found"})
    assert len(fake.starts) == 1


@pytest.mark.parametrize(
    ("fake", "code"),
    [
        (FakeTemporal({"error": "skill_failed"}), "skill_failed"),
        (FakeTemporal(RuntimeError("unexpected")), "tool_unavailable"),
        (FakeTemporal(delay=5), "skill_timeout"),
    ],
)
async def test_failures_are_bounded_tool_errors(hermes_env, temporal, monkeypatch, fake, code: str) -> None:
    monkeypatch.setattr(settings, "HERMES_SKILL_TIMEOUT_SECONDS", 0.2)
    skill_id = await _skill(hermes_env)
    temporal(fake)
    opened = await hermes_env.open_session()

    result = _tool_payload(await _rpc(hermes_env, opened["token"], _call("run_skill", {"skill_id": str(skill_id)})))

    assert result == (True, {"error": code})


async def test_large_output_is_truncated(hermes_env, temporal) -> None:
    skill_id = await _skill(hermes_env)
    temporal(FakeTemporal({"output": "x" * (hermes_skills.OUTPUT_CHARS + 10)}))
    opened = await hermes_env.open_session()

    _, payload = _tool_payload(await _rpc(hermes_env, opened["token"], _call("run_skill", {"skill_id": str(skill_id)})))

    assert payload["truncated"] is True and len(payload["output"]) == hermes_skills.OUTPUT_CHARS


async def test_a_run_between_turns_is_refused(hermes_env, temporal) -> None:
    skill_id = await _skill(hermes_env)
    fake = temporal(FakeTemporal())
    opened = await hermes_env.open_session()
    await hermes_env.internal("DELETE", f"/sessions/{opened['sessionId']}/turn")

    result = _tool_payload(await _rpc(hermes_env, opened["token"], _call("run_skill", {"skill_id": str(skill_id)})))

    assert result == (True, {"error": "hermes_turn_inactive"})
    assert fake.starts == []


@dataclass
class _Call:
    skill_id: str
    conversation_id: str
    session_token: str
    arguments: dict[str, Any]


@workflow.defn(name="HermesSkillCallWorkflow")
class StandInSkillWorkflow:
    """tool-worker's workflow, by name and shape (its own tests cover it)."""

    @workflow.run
    async def run(self, call: _Call) -> dict[str, Any]:
        result = await workflow.execute_activity(
            "execute_skill",
            args=(call.skill_id, call.arguments, call.conversation_id, call.session_token),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return {"output": result["output"]}


async def test_a_repeated_call_never_runs_the_skill_twice_on_temporal(hermes_env, monkeypatch) -> None:
    runs: list[str] = []

    @activity.defn(name="execute_skill")
    async def execute_skill(skill_id: str, args: dict[str, Any], conversation_id: str, token: str) -> dict[str, Any]:
        runs.append(skill_id)
        return {"skill_id": skill_id, "output": {"echo": args}}

    skill_id = await _skill(hermes_env)
    opened = await hermes_env.open_session()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        monkeypatch.setattr(hermes_skills, "get_temporal_client", lambda: env.client)
        async with Worker(
            env.client,
            task_queue="tools",
            workflows=[StandInSkillWorkflow],
            activities=[execute_skill],
            # The stand-in lives in this test module, which imports the app.
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            message = _call("run_skill", {"skill_id": str(skill_id), "arguments": {"q": "x"}})
            first = _tool_payload(await _rpc(hermes_env, opened["token"], message))
            again = _tool_payload(await _rpc(hermes_env, opened["token"], message))

    assert first == again == (False, {"skillId": str(skill_id), "output": '{"echo": {"q": "x"}}', "truncated": False})
    assert runs == [str(skill_id)]
