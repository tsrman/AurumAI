from __future__ import annotations

import asyncio
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).parents[2]
ORG_ID = "10000000-0000-4000-8000-000000000001"
ADMIN_ID = "10000000-0000-4000-8000-000000000002"
MEMBER_ID = "10000000-0000-4000-8000-000000000003"
CONVERSATION_ID = "10000000-0000-4000-8000-000000000004"
TURN_ID = "10000000-0000-4000-8000-000000000005"
EXECUTION_ID = "10000000-0000-4000-8000-000000000006"
OPERATION_ID = "10000000-0000-4000-8000-000000000007"
QUESTION_ID = "10000000-0000-4000-8000-000000000008"
RUN_ID = "10000000-0000-4000-8000-000000000009"
EVALUATION_RUN_ID = "10000000-0000-4000-8000-000000000010"


def load_gateway() -> ModuleType:
    path = ROOT / "scripts" / "k8s-test-gateway.py"
    spec = importlib.util.spec_from_file_location("user_questions_test_gateway", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_fixture_module() -> ModuleType:
    path = ROOT / "scripts" / "user_questions_e2e.py"
    spec = importlib.util.spec_from_file_location("user_questions_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_k8s_fixture_module() -> ModuleType:
    path = ROOT / "scripts" / "user_questions_k8s_e2e.py"
    spec = importlib.util.spec_from_file_location("user_questions_k8s_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def completion(app: Any, messages: list[dict[str, str]]) -> str:
    async def invoke() -> str:
        body = json.dumps(
            {"model": "question-test", "messages": messages, "stream": True}
        ).encode()
        request_sent = False
        response_complete = asyncio.Event()
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await response_complete.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                response_complete.set()

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/v1/chat/completions",
                "raw_path": b"/v1/chat/completions",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "client": ("test", 1234),
                "server": ("test", 80),
            },
            receive,
            send,
        )
        start = next(item for item in sent if item["type"] == "http.response.start")
        assert start["status"] == 200
        response_body = b"".join(
            item.get("body", b"")
            for item in sent
            if item["type"] == "http.response.body"
        ).decode()
        frames = [frame for frame in response_body.split("\n\n") if frame]
        assert frames[-1] == "data: [DONE]"
        chunks = [json.loads(frame.removeprefix("data: ")) for frame in frames[:-1]]
        return "".join(
            chunk["choices"][0].get("delta", {}).get("content", "")
            for chunk in chunks
        )

    return asyncio.run(invoke())


def ask_result(answer: dict[str, str]) -> dict[str, str]:
    return {
        "role": "user",
        "content": (
            "Tool result for ask_user: "
            + json.dumps({"tool": "ask_user", "output": answer})
        ),
    }


def test_ask_city_marker_uses_builtin_protocol_then_returns_the_answer() -> None:
    app = load_gateway().app
    initial = [
        {"role": "system", "content": "You may use ask_user."},
        {"role": "user", "content": "ASK_CITY_THEN_FINAL"},
    ]

    call = completion(app, initial)
    assert call == (
        '<tool_call>{"tool":"ask_user","args":{"text":"Which city?",'
        '"options":[]}}</tool_call>'
    )
    assert "skill_id" not in call

    final = completion(
        app,
        [
            *initial,
            {"role": "assistant", "content": call},
            ask_result({"text": "Kazan"}),
        ],
    )
    assert final == "CITY CONFIRMED: Kazan"


def test_ask_twice_marker_keeps_answers_correlated_by_round() -> None:
    app = load_gateway().app
    initial = [{"role": "user", "content": "ASK_TWICE"}]

    first = completion(app, initial)
    second_messages = [
        *initial,
        {"role": "assistant", "content": first},
        ask_result({"text": "Kazan"}),
    ]
    second = completion(app, second_messages)
    assert json.loads(second.removeprefix("<tool_call>").removesuffix("</tool_call>")) == {
        "tool": "ask_user",
        "args": {"text": "Which district?", "options": []},
    }

    final = completion(
        app,
        [
            *second_messages,
            {"role": "assistant", "content": second},
            ask_result({"text": "Vakhitovsky"}),
        ],
    )
    assert final == "LOCATION CONFIRMED: Kazan / Vakhitovsky"


def test_ask_loop_marker_uses_only_the_answer_after_its_marker() -> None:
    app = load_gateway().app
    initial = [
        ask_result({"text": "stale-answer"}),
        {"role": "user", "content": "ASK_LOOP"},
    ]

    call = completion(app, initial)
    final = completion(
        app,
        [
            *initial,
            {"role": "assistant", "content": call},
            ask_result({"text": "iteration-answer"}),
        ],
    )

    assert final == "LOOP CONFIRMED: iteration-answer"


def test_question_marker_does_not_leak_into_a_later_user_turn() -> None:
    app = load_gateway().app
    initial = [{"role": "user", "content": "ASK_CITY_THEN_FINAL"}]
    call = completion(app, initial)

    response = completion(
        app,
        [
            *initial,
            {"role": "assistant", "content": call},
            {"role": "user", "content": "POST_TIMEOUT_NEXT_TURN"},
        ],
    )

    assert call.startswith("<tool_call>")
    assert response == "Hello from the deterministic Kubernetes gateway."


def fake_executable(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
    path.chmod(0o755)


def run_wrapper(
    tmp_path: Path,
    primary_exit: int,
    cleanup_exit: int = 0,
    restore_exit: int = 0,
    verify_exit: int = 0,
    acceptance_kind: str = "short",
) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.log"
    prefix = 'printf "%s %s\\n" "$(basename "$0")" "$*" >> "$COMMAND_LOG"\n'
    fake_executable(
        fake_bin,
        "docker",
        prefix
        + """
if [[ "${1:-}" == inspect ]]; then
  if [[ "$*" == *com.docker.compose.service* ]]; then
    name="${2#aurum-ai-}"; [[ "$name" != postgres ]] || name=postgres
    printf '%s\n' "$name"
  else printf 'aurum-ai\n'; fi
fi
if [[ "$*" == *"--force-recreate backend"* && "$*" != *docker-compose.e2e.yml* ]]; then
  touch "$RESTORED_FLAG"; exit "$RESTORE"
fi
""",
    )
    fake_executable(
        fake_bin,
        "curl",
        prefix + 'if [[ -f "$RESTORED_FLAG" ]]; then exit "$READY"; fi\n',
    )
    fake_executable(fake_bin, "sleep", "exit 0\n")
    fake_executable(
        fake_bin,
        "npm",
        prefix
        + 'printf "mode %s\\n" "$QUESTION_ACCEPTANCE_KIND" >> "$COMMAND_LOG"\n'
        + 'exit "$PRIMARY"\n',
    )
    fake_executable(
        fake_bin,
        "python3",
        prefix
        + """
if [[ "${1:-}" == -c ]]; then printf '0123456789abcdef\n'; exit 0; fi
command="$2"; shift 2
while (( $# )); do case "$1" in --state) state="$2"; shift 2;; *) shift;; esac; done
if [[ "$command" == prepare ]]; then printf '{"marker":"owned"}\n' > "$state"; fi
if [[ "$command" == cleanup ]]; then exit "$CLEANUP"; fi
if [[ "$command" == verify-clean ]]; then exit "$VERIFY"; fi
""",
    )
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "APP_SECRET_KEY": "question-wrapper-test-key-0123456789abcdef",
        "COMMAND_LOG": str(log),
        "PRIMARY": str(primary_exit),
        "CLEANUP": str(cleanup_exit),
        "RESTORE": str(restore_exit),
        "VERIFY": str(verify_exit),
        "READY": "0",
        "QUESTION_ACCEPTANCE_KIND": acceptance_kind,
        "RESTORED_FLAG": str(tmp_path / "restored"),
    }
    result = subprocess.run(
        ["bash", str(ROOT / "smoke-test-user-questions.sh")],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    commands = log.read_text().splitlines() if log.exists() else []
    prepare = next((line for line in commands if " prepare " in line), "")
    state = (
        Path(prepare.split("--state ", 1)[1].split(" --marker", 1)[0])
        if prepare
        else tmp_path / "missing-state.json"
    )
    return result, commands, state


@pytest.mark.parametrize(
    "primary,cleanup,restore,verify,want",
    [
        (0, 0, 0, 0, 0),
        (7, 0, 0, 0, 7),
        (0, 9, 0, 0, 9),
        (0, 0, 11, 0, 11),
        (0, 0, 0, 12, 12),
        (7, 9, 11, 12, 7),
    ],
)
def test_wrapper_preserves_primary_exit_and_retains_failed_recovery(
    tmp_path: Path,
    primary: int,
    cleanup: int,
    restore: int,
    verify: int,
    want: int,
) -> None:
    result, commands, state = run_wrapper(
        tmp_path,
        primary,
        cleanup_exit=cleanup,
        restore_exit=restore,
        verify_exit=verify,
    )

    assert result.returncode == want, result.stderr
    assert state.exists() == bool(want)
    assert any(" cleanup " in command for command in commands)
    assert any(" verify-clean " in command for command in commands)
    assert any(
        "--force-recreate backend backend-admin" in command
        and "docker-compose.e2e.yml" not in command
        for command in commands
    )
    assert any("inspect aurum-ai-backend-admin" in command for command in commands)
    assert any(
        "e2e/user-questions.real.spec.ts" in command
        and "--workers=1" in command
        and "--trace=off" in command
        for command in commands
    )
    if want:
        assert "recovery state retained" in result.stderr
        assert "verify-clean --state" in result.stderr


def test_wrapper_validates_and_forwards_acceptance_kind(tmp_path: Path) -> None:
    result, commands, _ = run_wrapper(
        tmp_path, primary_exit=0, acceptance_kind="hour"
    )

    assert result.returncode == 0, result.stderr
    assert "mode hour" in commands
    assert "QUESTION_TIMEOUT" not in (ROOT / "smoke-test-user-questions.sh").read_text()


def test_hour_browser_budget_tolerates_wsl_monotonic_clock_drift() -> None:
    browser_test = (ROOT / "business-ui/e2e/user-questions.real.spec.ts").read_text()

    assert 'KIND === "hour" ? 4_800_000' in browser_test
    assert "timeout: 4_500_000" in browser_test
    assert "3_660_000" not in browser_test


def test_k8s_browser_budget_covers_slow_wsl_port_forward_round_trips() -> None:
    browser_test = (ROOT / "business-ui/e2e/user-questions.real.spec.ts").read_text()

    assert "IS_KUBERNETES ? 600_000 : 300_000" in browser_test


def test_browser_answer_step_has_bounded_action_and_response_diagnostics() -> None:
    browser_test = (ROOT / "business-ui/e2e/user-questions.real.spec.ts").read_text()

    assert "const ANSWER_ACTION_TIMEOUT = 30_000" in browser_test
    assert "const ANSWER_RESPONSE_TIMEOUT = 90_000" in browser_test
    assert "{ timeout: ANSWER_RESPONSE_TIMEOUT }" in browser_test
    assert ".click({ timeout: ANSWER_ACTION_TIMEOUT })" in browser_test


def test_pending_question_wait_covers_runner_reconciliation_interval() -> None:
    browser_test = (ROOT / "business-ui/e2e/user-questions.real.spec.ts").read_text()

    assert "const PENDING_QUESTION_TIMEOUT = 90_000" in browser_test
    assert "timeout: PENDING_QUESTION_TIMEOUT" in browser_test
    assert "intervals: [500]" in browser_test


def test_wrapper_rejects_unknown_acceptance_kind_before_mutation(tmp_path: Path) -> None:
    result, commands, _ = run_wrapper(
        tmp_path, primary_exit=0, acceptance_kind="accelerated-hour"
    )

    assert result.returncode != 0
    assert commands == []


def test_wrapper_refuses_overlap_before_mutation(tmp_path: Path) -> None:
    lock_path = tmp_path / "aurum-ai-user-questions-e2e.lock"
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result, commands, _ = run_wrapper(tmp_path, primary_exit=0)

    assert result.returncode != 0
    assert commands == []


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, Any]] = []

    def token(self, username: str, _password: str, audience: str) -> str:
        return f"{username}:{audience}"

    def request(
        self,
        plane: str,
        method: str,
        path: str,
        token: str,
        *,
        body: Any = None,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        del expected
        self.calls.append((plane, method, path, body))
        if path == "/v1/auth/me":
            user_id = ADMIN_ID if token.startswith("admin:") else MEMBER_ID
            return {"data": {"org_id": ORG_ID, "user_id": user_id}}
        if method == "PUT" and path.endswith("/reconcile"):
            return {"data": {"updated": True}}
        if method == "DELETE":
            return None
        raise AssertionError((plane, method, path, token, body))


class FakeDatabase:
    def __init__(self, inventory: dict[str, Any]) -> None:
        self.data = inventory
        self.deleted: list[str] = []
        self.restored: list[str] = []
        self.deferred: list[tuple[str, int]] = []

    def inventory(
        self, org_id: str, model_id: str, marker: str | None = None
    ) -> dict[str, Any]:
        assert org_id == ORG_ID
        assert model_id.endswith("-model")
        del marker
        return self.data

    def delete_conversation(self, state: Any, row: dict[str, Any]) -> None:
        self.deleted.append(row["id"])
        self.data = {
            **self.data,
            "conversation": [],
            "turn": [],
            "execution": [],
            "operation": [],
            "question": [],
            "outbox": [],
        }

    def restore_model_grants(self, state: Any, model_id: str) -> None:
        self.restored.append(model_id)
        self.data = {**self.data, "model_grants": state.original_model_grants}

    def defer_delivery(self, state: Any, question_id: str, seconds: int) -> None:
        assert state.organization_id == ORG_ID
        self.deferred.append((question_id, seconds))


def empty_inventory() -> dict[str, Any]:
    return {
        "conversation": [],
        "turn": [],
        "execution": [],
        "operation": [],
        "question": [],
        "outbox": [],
        "model_grants": [],
    }


def owned_inventory(marker: str) -> dict[str, Any]:
    return {
        "conversation": [
            {
                "id": CONVERSATION_ID,
                "org_id": ORG_ID,
                "user_id": MEMBER_ID,
                "title": f"{marker} ASK_CITY_THEN_FINAL"[:60],
                "status": "active",
                "has_attachments": False,
                "has_workflow_runs": False,
            }
        ],
        "turn": [
            {
                "id": TURN_ID,
                "org_id": ORG_ID,
                "conversation_id": CONVERSATION_ID,
                "owner_id": MEMBER_ID,
                "status": "active",
            }
        ],
        "execution": [
            {
                "id": EXECUTION_ID,
                "org_id": ORG_ID,
                "turn_id": TURN_ID,
                "workflow_id": f"conversation-{CONVERSATION_ID}",
                "run_id": RUN_ID,
                "task_queue": f"session-{CONVERSATION_ID}",
                "phase": "ordinary",
                "node_key": None,
                "agent_definition_id": None,
                "iteration": 1,
            }
        ],
        "operation": [
            {
                "id": OPERATION_ID,
                "org_id": ORG_ID,
                "turn_id": TURN_ID,
                "execution_id": EXECUTION_ID,
                "kind": "ask_user",
            }
        ],
        "question": [
            {
                "id": QUESTION_ID,
                "org_id": ORG_ID,
                "turn_id": TURN_ID,
                "execution_id": EXECUTION_ID,
                "operation_id": OPERATION_ID,
                "text": "Which city?",
                "status": "pending",
            }
        ],
        "outbox": [],
        "model_grants": [],
    }


def conversation_only_inventory(marker: str) -> dict[str, Any]:
    inventory = owned_inventory(marker)
    return {
        **inventory,
        "turn": [],
        "execution": [],
        "operation": [],
        "question": [],
        "outbox": [],
    }


def test_prepare_ignores_unrelated_resources_in_the_shared_organization(
    tmp_path: Path,
) -> None:
    fixture = load_fixture_module()
    marker = "user-questions-e2e-0123456789abcdef"
    state_path = tmp_path / "journal" / "state.json"
    unrelated = owned_inventory("somebody-elses-fixture")
    controller = fixture.FixtureController(
        fixture.Config(),
        state_path,
        marker,
        api=FakeApi(),
        database=FakeDatabase(unrelated),
    )

    controller.prepare()

    assert fixture.load_state(state_path).prepared is True


def test_pending_question_revoke_restores_only_owned_model_grant(tmp_path: Path) -> None:
    fixture = load_fixture_module()
    marker = "user-questions-e2e-0123456789abcdef"
    path = tmp_path / "journal" / "state.json"
    api = FakeApi()
    controller = fixture.FixtureController(fixture.Config(), path, marker,
                                           api=api, database=FakeDatabase(empty_inventory()))
    controller.prepare()
    controller.revoke()
    controller.grant()
    writes = [call[3] for call in api.calls if call[1] == "PUT"]
    assert writes[-2] == {"resourceType": "model", "resourceId": marker + "-model", "grants": []}
    assert writes[-1] == {"resourceType": "model", "resourceId": marker + "-model",
                          "grants": [{"principalType": "user", "principalId": MEMBER_ID, "permission": "read"}]}
    assert fixture.load_state(path).original_model_grants == []


def test_prepare_snapshots_fresh_org_provisioning_grant(tmp_path: Path) -> None:
    fixture = load_fixture_module()
    marker = "user-questions-e2e-0123456789abcdef"
    path = tmp_path / "journal" / "state.json"
    api = FakeApi()
    inventory = empty_inventory()
    provisioning_grant = {
        "principalType": "organization",
        "principalId": ORG_ID,
        "permission": "read",
    }
    inventory["model_grants"] = [provisioning_grant]
    controller = fixture.FixtureController(
        fixture.Config(), path, marker, api=api, database=FakeDatabase(inventory)
    )

    controller.prepare()
    controller.revoke()
    controller.grant()

    state = fixture.load_state(path)
    assert state.original_model_grants == [provisioning_grant]
    writes = [call[3] for call in api.calls if call[1] == "PUT"]
    assert writes == [
        {
            "resourceType": "model",
            "resourceId": marker + "-model",
            "grants": [
                {
                    "principalType": "user",
                    "principalId": MEMBER_ID,
                    "permission": "read",
                }
            ],
        },
        {"resourceType": "model", "resourceId": marker + "-model", "grants": []},
        {
            "resourceType": "model",
            "resourceId": marker + "-model",
            "grants": [
                {
                    "principalType": "user",
                    "principalId": MEMBER_ID,
                    "permission": "read",
                }
            ],
        },
    ]


def test_question_revoke_rejects_different_journal_owner(tmp_path: Path) -> None:
    fixture = load_fixture_module()
    marker = "user-questions-e2e-0123456789abcdef"
    path = tmp_path / "journal" / "state.json"
    api = FakeApi()
    controller = fixture.FixtureController(fixture.Config(), path, marker,
                                           api=api, database=FakeDatabase(empty_inventory()))
    controller.prepare()
    other = fixture.FixtureController(fixture.Config(), path,
                                      "user-questions-e2e-1111111111111111", api=api,
                                      database=FakeDatabase(empty_inventory()))
    before = len(api.calls)
    with pytest.raises(fixture.FixtureError, match="marker mismatch"):
        other.revoke()
    assert len(api.calls) == before


def test_inventory_type_error_is_not_retried_as_a_signature_fallback(
    tmp_path: Path,
) -> None:
    fixture = load_fixture_module()

    class BrokenDatabase:
        calls = 0

        def inventory(self, *args: Any) -> dict[str, Any]:
            del args
            self.calls += 1
            raise TypeError("database decoder failed")

    database = BrokenDatabase()
    controller = fixture.FixtureController(
        fixture.Config(),
        tmp_path / "journal" / "state.json",
        "user-questions-e2e-0123456789abcdef",
        api=FakeApi(),
        database=database,
    )

    with pytest.raises(TypeError, match="database decoder failed"):
        controller.prepare()

    assert database.calls == 1


def test_fixture_records_server_owned_interaction_chain_in_private_journal(
    tmp_path: Path,
) -> None:
    fixture = load_fixture_module()
    marker = "user-questions-e2e-0123456789abcdef"
    state_path = tmp_path / "journal" / "state.json"
    api = FakeApi()
    database = FakeDatabase(empty_inventory())
    controller = fixture.FixtureController(
        fixture.Config(),
        state_path,
        marker,
        api=api,
        database=database,
    )

    controller.prepare()
    database.data = owned_inventory(marker)
    controller.record("question", QUESTION_ID)

    state = fixture.load_state(state_path)
    assert state.conversation_ids == [CONVERSATION_ID]
    assert state.turn_ids == [TURN_ID]
    assert state.execution_ids == [EXECUTION_ID]
    assert state.operation_ids == [OPERATION_ID]
    assert state.question_ids == [QUESTION_ID]
    assert state.temporal_runs == {f"conversation-{CONVERSATION_ID}": RUN_ID}
    assert state_path.stat().st_mode & 0o777 == 0o600
    assert "password" not in state_path.read_text().lower()
    assert api.calls[-1][1:3] == (
        "PUT",
        "/v1/admin/authorization/grants/reconcile",
    )


class FakeRunnerCleaner:
    def __init__(self) -> None:
        self.removed: list[str] = []
        self.absent: list[str] = []
        self.stopped: list[str] = []
        self.started: list[str] = []

    def remove_owned(self, conversation_id: str) -> None:
        self.removed.append(conversation_id)

    def assert_absent(self, conversation_id: str) -> None:
        self.absent.append(conversation_id)

    def stop_owned(self, conversation_id: str) -> None:
        self.stopped.append(conversation_id)

    def start_owned(self, conversation_id: str) -> None:
        self.started.append(conversation_id)


class FakeTemporal:
    def __init__(self, root: dict[str, Any]) -> None:
        self.root = root
        self.terminated: list[tuple[str, str]] = []

    def read(self, workflow_id: str, run_id: str | None = None) -> Any:
        if workflow_id == self.root["workflow_id"] and (
            run_id is None or run_id == self.root["run_id"]
        ):
            return self.root
        child = next(
            (
                value
                for value in self.root.get("child_records", [])
                if value["workflow_id"] == workflow_id
                and (run_id is None or value["run_id"] == run_id)
            ),
            None,
        )
        return child

    def terminate(self, workflow_id: str, run_id: str) -> None:
        self.terminated.append((workflow_id, run_id))
        if workflow_id == self.root["workflow_id"] and run_id == self.root["run_id"]:
            self.root = {**self.root, "status": "TERMINATED"}


def temporal_root(*, children: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "workflow_id": f"conversation-{CONVERSATION_ID}",
        "run_id": RUN_ID,
        "workflow_type": "ConversationWorkflow",
        "task_queue": f"session-{CONVERSATION_ID}",
        "status": "RUNNING",
        "input": {"conversation_id": CONVERSATION_ID},
        "parent": None,
        "children": children or [],
        "continued": False,
        "pending_children": False,
        "child_records": [],
    }


def test_cleanup_fails_closed_before_mutation_for_a_foreign_temporal_child(
    tmp_path: Path,
) -> None:
    fixture = load_fixture_module()
    marker = "user-questions-e2e-0123456789abcdef"
    state_path = tmp_path / "journal" / "state.json"
    api = FakeApi()
    database = FakeDatabase(empty_inventory())
    runner = FakeRunnerCleaner()
    root = temporal_root(
        children=[
            {
                "workflow_id": "foreign-workflow",
                "run_id": "20000000-0000-4000-8000-000000000001",
                "initiated_event_id": 42,
            }
        ]
    )
    temporal = FakeTemporal(root)
    controller = fixture.FixtureController(
        fixture.Config(),
        state_path,
        marker,
        api=api,
        database=database,
        runner_cleaner=runner,
        temporal=temporal,
    )
    controller.prepare()
    database.data = owned_inventory(marker)
    controller.record("question", QUESTION_ID)

    with pytest.raises(fixture.FixtureError, match="Temporal execution is not journal-owned"):
        controller.cleanup()

    assert not temporal.terminated
    assert not database.deleted
    assert not runner.removed
    assert not any(method == "DELETE" for _, method, _, _ in api.calls)
    assert state_path.exists()


def test_cleanup_closes_exact_root_before_deleting_and_verifies_restoration(
    tmp_path: Path,
) -> None:
    fixture = load_fixture_module()
    marker = "user-questions-e2e-0123456789abcdef"
    state_path = tmp_path / "journal" / "state.json"
    api = FakeApi()
    database = FakeDatabase(empty_inventory())
    runner = FakeRunnerCleaner()
    temporal = FakeTemporal(temporal_root())
    controller = fixture.FixtureController(
        fixture.Config(),
        state_path,
        marker,
        api=api,
        database=database,
        runner_cleaner=runner,
        temporal=temporal,
    )
    controller.prepare()
    database.data = owned_inventory(marker)
    controller.record("question", QUESTION_ID)

    controller.cleanup()
    controller.verify_clean()

    assert temporal.terminated == [
        (f"conversation-{CONVERSATION_ID}", RUN_ID)
    ]
    assert database.deleted == [CONVERSATION_ID]
    assert runner.removed == [CONVERSATION_ID]
    assert runner.absent == [CONVERSATION_ID]
    assert any(
        plane == "business"
        and method == "DELETE"
        and path == f"/v1/conversations/{CONVERSATION_ID}"
        for plane, method, path, _ in api.calls
    )
    reconciles = [
        body
        for _, method, path, body in api.calls
        if method == "PUT" and path.endswith("/reconcile")
    ]
    assert reconciles[-1]["grants"] == []


def test_cleanup_discovers_exact_root_when_question_registration_never_finished(
    tmp_path: Path,
) -> None:
    fixture = load_fixture_module()
    marker = "user-questions-e2e-0123456789abcdef"
    state_path = tmp_path / "journal" / "state.json"
    database = FakeDatabase(empty_inventory())
    temporal = FakeTemporal(temporal_root())
    controller = fixture.FixtureController(
        fixture.Config(),
        state_path,
        marker,
        api=FakeApi(),
        database=database,
        runner_cleaner=FakeRunnerCleaner(),
        temporal=temporal,
    )
    controller.prepare()
    database.data = conversation_only_inventory(marker)
    controller.record("conversation", CONVERSATION_ID)

    controller.cleanup()
    controller.verify_clean()

    assert temporal.terminated == [
        (f"conversation-{CONVERSATION_ID}", RUN_ID)
    ]
    state = fixture.load_state(state_path)
    assert state.temporal_runs == {f"conversation-{CONVERSATION_ID}": RUN_ID}


def test_restart_and_delivery_fault_target_only_recorded_owned_chain(
    tmp_path: Path,
) -> None:
    fixture = load_fixture_module()
    marker = "user-questions-e2e-0123456789abcdef"
    state_path = tmp_path / "journal" / "state.json"
    database = FakeDatabase(empty_inventory())
    runner = FakeRunnerCleaner()
    controller = fixture.FixtureController(
        fixture.Config(),
        state_path,
        marker,
        api=FakeApi(),
        database=database,
        runner_cleaner=runner,
        temporal=FakeTemporal(temporal_root()),
    )
    controller.prepare()
    database.data = owned_inventory(marker)
    controller.record("question", QUESTION_ID)

    controller.stop_worker(CONVERSATION_ID)
    controller.defer_delivery(QUESTION_ID, 10)
    controller.start_worker(CONVERSATION_ID)

    assert runner.stopped == [CONVERSATION_ID]
    assert runner.started == [CONVERSATION_ID]
    assert database.deferred == [(QUESTION_ID, 10)]
    with pytest.raises(fixture.FixtureError, match="recorded owned conversation"):
        controller.stop_worker("20000000-0000-4000-8000-000000000001")
    assert runner.stopped == [CONVERSATION_ID]


def test_cleanup_accepts_only_reciprocal_exact_evaluation_child(tmp_path: Path) -> None:
    fixture = load_fixture_module()
    marker = "user-questions-e2e-0123456789abcdef"
    state_path = tmp_path / "journal" / "state.json"
    api = FakeApi()
    database = FakeDatabase(empty_inventory())
    runner = FakeRunnerCleaner()
    child_link = {
        "workflow_id": f"{CONVERSATION_ID}-eval-{TURN_ID}",
        "run_id": EVALUATION_RUN_ID,
        "initiated_event_id": 42,
    }
    root = temporal_root(children=[child_link])
    root["child_records"] = [
        {
            "workflow_id": child_link["workflow_id"],
            "run_id": EVALUATION_RUN_ID,
            "workflow_type": "EvaluationWorkflow",
            "task_queue": f"session-{CONVERSATION_ID}",
            "status": "COMPLETED",
            "input": {
                "conversation_id": CONVERSATION_ID,
                "turn_id": TURN_ID,
            },
            "parent": {
                "workflow_id": f"conversation-{CONVERSATION_ID}",
                "run_id": RUN_ID,
                "initiated_event_id": 42,
            },
            "children": [],
            "continued": False,
            "pending_children": False,
        }
    ]
    controller = fixture.FixtureController(
        fixture.Config(),
        state_path,
        marker,
        api=api,
        database=database,
        runner_cleaner=runner,
        temporal=FakeTemporal(root),
    )
    controller.prepare()
    database.data = owned_inventory(marker)
    controller.record("question", QUESTION_ID)

    controller.cleanup()
    controller.verify_clean()

    assert database.deleted == [CONVERSATION_ID]


class FakePodMetadata:
    def __init__(
        self,
        *,
        name: str = f"runner-{CONVERSATION_ID}",
        namespace: str = "owned-runners",
        uid: str = "30000000-0000-4000-8000-000000000001",
        labels: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.namespace = namespace
        self.uid = uid
        self.labels = labels or {
            "app.kubernetes.io/instance": "owned-release",
            "aurum-ai.component": "conversation-runner",
            "aurum-ai.conversation": CONVERSATION_ID,
        }


class FakePod:
    def __init__(self, metadata: FakePodMetadata | None = None) -> None:
        self.metadata = metadata or FakePodMetadata()


class FakeCoreV1:
    def __init__(self, pod: FakePod | None | object = ...) -> None:
        self.pod = FakePod() if pod is ... else pod
        self.deleted: list[tuple[str, str, Any]] = []

    async def read_namespaced_pod(self, name: str, namespace: str) -> FakePod:
        if self.pod is None:
            raise FakeKubernetesApiError(404)
        assert name == self.pod.metadata.name
        assert namespace == self.pod.metadata.namespace
        return self.pod

    async def delete_namespaced_pod(
        self, name: str, namespace: str, body: Any
    ) -> None:
        self.deleted.append((name, namespace, body))
        self.pod = None


class FakeKubernetesApiError(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


def test_k8s_runner_cleanup_pins_and_deletes_only_exact_uid(tmp_path: Path) -> None:
    fixture = load_fixture_module()
    k8s_fixture = load_k8s_fixture_module()
    state_path = tmp_path / "journal" / "state.json"
    state = fixture.FixtureState(
        marker="user-questions-e2e-0123456789abcdef",
        organization_id=ORG_ID,
        admin_id=ADMIN_ID,
        member_id=MEMBER_ID,
        conversation_ids=[CONVERSATION_ID],
    )
    fixture.save_state(state_path, state)
    api = FakeCoreV1()
    control = k8s_fixture.KubernetesPodControl(
        namespace="owned-runners",
        release="owned-release",
        state_path=state_path,
        core_v1=api,
        api_error=FakeKubernetesApiError,
    )

    control.pin_owned(CONVERSATION_ID)
    pinned = fixture.load_state(state_path).runner_resources[CONVERSATION_ID]
    assert pinned == {
        "kind": "kubernetes",
        "name": f"runner-{CONVERSATION_ID}",
        "namespace": "owned-runners",
        "release": "owned-release",
        "uid": "30000000-0000-4000-8000-000000000001",
    }

    control.remove_owned(CONVERSATION_ID)

    assert len(api.deleted) == 1
    name, namespace, body = api.deleted[0]
    assert (name, namespace) == (f"runner-{CONVERSATION_ID}", "owned-runners")
    assert body.preconditions.uid == pinned["uid"]


@pytest.mark.parametrize(
    "metadata",
    [
        FakePodMetadata(uid="30000000-0000-4000-8000-000000000099"),
        FakePodMetadata(
            labels={
                "app.kubernetes.io/instance": "foreign-release",
                "aurum-ai.component": "conversation-runner",
                "aurum-ai.conversation": CONVERSATION_ID,
            }
        ),
        FakePodMetadata(
            labels={
                "app.kubernetes.io/instance": "owned-release",
                "aurum-ai.component": "conversation-runner",
                "aurum-ai.conversation": "20000000-0000-4000-8000-000000000001",
            }
        ),
    ],
)
def test_k8s_runner_cleanup_fails_closed_for_replaced_or_foreign_pod(
    tmp_path: Path, metadata: FakePodMetadata
) -> None:
    fixture = load_fixture_module()
    k8s_fixture = load_k8s_fixture_module()
    state_path = tmp_path / "journal" / "state.json"
    state = fixture.FixtureState(
        marker="user-questions-e2e-0123456789abcdef",
        organization_id=ORG_ID,
        admin_id=ADMIN_ID,
        member_id=MEMBER_ID,
        conversation_ids=[CONVERSATION_ID],
        runner_resources={
            CONVERSATION_ID: {
                "kind": "kubernetes",
                "name": f"runner-{CONVERSATION_ID}",
                "namespace": "owned-runners",
                "release": "owned-release",
                "uid": "30000000-0000-4000-8000-000000000001",
            }
        },
    )
    fixture.save_state(state_path, state)
    api = FakeCoreV1(FakePod(metadata))
    control = k8s_fixture.KubernetesPodControl(
        namespace="owned-runners",
        release="owned-release",
        state_path=state_path,
        core_v1=api,
        api_error=FakeKubernetesApiError,
    )

    with pytest.raises(
        k8s_fixture.FixtureError, match="Kubernetes runner ownership mismatch"
    ):
        control.remove_owned(CONVERSATION_ID)

    assert api.deleted == []


def test_k8s_runner_cleanup_refuses_unpinned_live_pod(tmp_path: Path) -> None:
    fixture = load_fixture_module()
    k8s_fixture = load_k8s_fixture_module()
    state_path = tmp_path / "journal" / "state.json"
    fixture.save_state(
        state_path,
        fixture.FixtureState(
            marker="user-questions-e2e-0123456789abcdef",
            organization_id=ORG_ID,
            admin_id=ADMIN_ID,
            member_id=MEMBER_ID,
            conversation_ids=[CONVERSATION_ID],
        ),
    )
    api = FakeCoreV1()
    control = k8s_fixture.KubernetesPodControl(
        namespace="owned-runners",
        release="owned-release",
        state_path=state_path,
        core_v1=api,
        api_error=FakeKubernetesApiError,
    )

    with pytest.raises(k8s_fixture.FixtureError, match="not pinned"):
        control.remove_owned(CONVERSATION_ID)

    assert api.deleted == []


def test_k8s_launcher_uses_isolated_release_and_exact_fixture_adapter() -> None:
    launcher = (ROOT / "smoke-test-user-questions-k8s.sh").read_text()
    shared = (ROOT / "smoke-test-k8s.sh").read_text()

    assert "K8S_TEST_ACCEPTANCE=user-questions" in launcher
    assert "K8S_TEST_CLUSTER" in launcher
    assert "K8S_TEST_RELEASE" in launcher
    assert "QUESTION_ACCEPTANCE_KIND=short" in launcher
    assert "user_questions_k8s_e2e.py" in shared
    assert "userQuestionsEnabled:" in shared
    assert "E2E_USER_QUESTIONS_FIXTURE_SCRIPT" in shared
    assert "aurum-ai-user-questions-e2e.lock" in shared
    assert "TEST_GATEWAY_MODEL_ID_YAML" in shared
    assert "json.dumps" in shared
    assert 'value: "$TEST_GATEWAY_MODEL_ID"' not in shared
    # Browser URL/rendered ingress wiring is executed by test_k8s_browser_ingress.
    assert 'keycloakUrl: $KEYCLOAK_PUBLIC_URL' in shared
    assert 'INGRESS_NODE_PORT=30080' in shared
    assert '127.0.0.1:$FRONTEND_PORT:$INGRESS_NODE_PORT@loadbalancer' in shared
    assert '--disable=servicelb@server:*' in shared
    assert 'patch service traefik --type merge' in shared
    assert '"nodePort":30080' in shared
    assert '127.0.0.1:$FRONTEND_PORT:80@loadbalancer' not in shared
