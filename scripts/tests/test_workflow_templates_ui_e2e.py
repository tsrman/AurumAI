from __future__ import annotations

import importlib.util
import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[2]
HARNESS = ROOT / "scripts/workflow_templates_ui_e2e.py"
MARKER = "workflow-templates-ui-e2e-0123456789abcdef"
ORG, ADMIN, MEMBER, A1, A2, SOURCE, TEMPLATE, COPY, CLONE, CONVERSATION, RUN = (
    f"{i:08x}-1111-4111-8111-111111111111" for i in range(1, 12)
)


@pytest.fixture
def h():
    assert HARNESS.exists(), "template recovery harness is not implemented"
    spec = importlib.util.spec_from_file_location("templates_harness", HARNESS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def state(h):
    return h.FixtureState(
        marker=MARKER,
        organization_id=ORG,
        admin_id=ADMIN,
        member_id=MEMBER,
        original_capabilities=["chat.send"],
        original_model_grants=[],
        prepared=True,
    )


@pytest.mark.parametrize("marker", ["foreign", MARKER[:-1], MARKER + "a", MARKER.upper()])
def test_invalid_marker_cannot_start_cleanup(h, tmp_path, marker):
    with pytest.raises(h.FixtureError, match="marker"):
        h.FixtureController(h.Config(), tmp_path / "state.json", marker)


def test_private_strict_journal_and_shared_parent_refusal(h, tmp_path):
    path = tmp_path / "private/state.json"
    h.save_state(path, state(h))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert h.load_state(path) == state(h)
    for forbidden in ("token", "password", "secret"):
        assert forbidden not in path.read_text().lower()
    raw = json.loads(path.read_text())
    raw["access_token"] = "never-load"
    path.write_text(json.dumps(raw))
    with pytest.raises(h.FixtureError, match="schema"):
        h.load_state(path)
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)  # Set the intended shared mode independently of caller umask.
    with pytest.raises(h.FixtureError, match="private"):
        h.save_state(shared / "state.json", state(h))
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755


@pytest.mark.parametrize(
    "field,value",
    [
        ("organization_id", "bad"),
        ("agent_ids", ["bad"]),
        ("prepared", "yes"),
        ("original_capabilities", "chat.send"),
    ],
)
def test_invalid_journal_types_fail_before_mutation(h, tmp_path, field, value):
    data = state(h)
    setattr(data, field, value)
    with pytest.raises(h.FixtureError):
        h.save_state(tmp_path / "private/state.json", data)


def row(id, name, **extra):
    return {"id": id, "name": name, "org_id": ORG, "created_by": ADMIN, **extra}


def inventory():
    return {
        "agent": [
            row(A1, f"{MARKER}-source-v1"),
            row(A2, f"{MARKER}-source-v2"),
            row(CLONE, "x" * 216 + f" [{CLONE}]"),
        ],
        "workflow": [
            row(
                SOURCE,
                f"{MARKER}-source",
                source_template_id=None,
                source_template_version=None,
                agent_ids=[A1],
            ),
            row(
                COPY,
                f"{MARKER}-copy-a",
                source_template_id=TEMPLATE,
                source_template_version=1,
                agent_ids=[CLONE],
            ),
        ],
        "template": [row(TEMPLATE, f"{MARKER}-template")],
        "conversation": [
            {"id": CONVERSATION, "title": f"{MARKER}-run-v1", "org_id": ORG, "user_id": MEMBER}
        ],
        "run": [
            {
                "id": RUN,
                "org_id": ORG,
                "initiating_user_id": MEMBER,
                "conversation_id": CONVERSATION,
                "workflow_definition_id": COPY,
            }
        ],
        "capabilities": ["chat.send"],
        "model_grants": [],
    }


def test_recovery_discovers_third_fixed_loop_conversation(h, tmp_path):
    c = controller(h, tmp_path)
    c.database.data["conversation"][0]["title"] = f"{MARKER}-run-v3"

    owned = c.discover(h.load_state(c.state_path), c.database.data)

    assert [row["id"] for row in owned["conversation"]] == [CONVERSATION]


class Database:
    def __init__(self, data=None):
        self.data = inventory() if data is None else data
        self.deleted = []
        self.fail = False

    def inventory(self, org, model):
        assert org == ORG and model == f"{MARKER}-model"
        return self.data

    def delete_owned(self, state, owned):
        if self.fail:
            raise RuntimeError("simulated delete failure")
        self.deleted.append(owned)
        for kind in ("workflow", "template", "conversation", "run"):
            ids = {r["id"] for r in owned[kind]}
            self.data[kind] = [r for r in self.data[kind] if r["id"] not in ids]

    def restore_model_grants(self, state, model):
        assert state.original_model_grants == []
        assert model == f"{MARKER}-model"
        self.data["model_grants"] = []


class Api:
    def __init__(self, db):
        self.db = db
        self.calls = []
        self.deleted_conversations = []

    def token(self, username, password, audience):
        assert audience in {"admin-ui", "business-ui"}
        return audience + ":" + username

    def request(self, plane, method, path, token, *, body=None, expected=(200,)):
        self.calls.append((plane, method, path, body))
        if method == "PUT" and path.endswith("capabilities/organization"):
            self.db.data["capabilities"] = body["capabilities"]
        elif method == "PUT" and path.endswith("grants/reconcile"):
            if body["resourceType"] == "model":
                self.db.data["model_grants"] = body["grants"]
        elif method == "DELETE" and "/agent-definitions/" in path:
            self.db.data["agent"] = [
                r for r in self.db.data["agent"] if r["id"] != path.rsplit("/", 1)[1]
            ]
        elif method == "DELETE":
            assert path == f"/v1/conversations/{CONVERSATION}", "unregistered delete route"
            assert plane == "business"
            assert token == "business-ui:user"
            self.deleted_conversations.append((plane, path, token))
        return {"data": {}}


class Runner:
    def __init__(self):
        self.removed = []

    def remove_owned(self, id):
        self.removed.append(id)

    def assert_absent(self, id):
        assert id in self.removed


ROOT_RUN, CHILD_RUN, PREP_RUN = (f"{i:08x}-2222-4222-8222-222222222222" for i in range(1, 4))
ROOT_ID = f"conversation-{CONVERSATION}"
CHILD_ID = f"{RUN}:orchestrator"
PREP_ID = f"{RUN}:supervisor_prepare"


class TemporalTransport:
    """External RPC double; ownership/traversal/closure policy stays real."""

    def __init__(self):
        def execution(id, run, kind, inputs, parent=None, children=(), status="RUNNING"):
            return dict(
                workflow_id=id,
                run_id=run,
                workflow_type=kind,
                task_queue=f"session-{CONVERSATION}",
                input=inputs,
                parent=parent,
                children=list(children),
                status=status,
                continued=False,
                pending_children=False,
            )

        self.rows = {
            ROOT_ID: execution(
                ROOT_ID,
                ROOT_RUN,
                "ConversationWorkflow",
                {"conversation_id": CONVERSATION},
                children=[dict(workflow_id=CHILD_ID, run_id=CHILD_RUN, initiated_event_id=12)],
            ),
            CHILD_ID: execution(
                CHILD_ID,
                CHILD_RUN,
                "MultiAgentWorkflow",
                {"run_id": RUN},
                dict(workflow_id=ROOT_ID, run_id=ROOT_RUN, initiated_event_id=12),
                children=[dict(workflow_id=PREP_ID, run_id=PREP_RUN, initiated_event_id=35)],
            ),
            PREP_ID: execution(
                PREP_ID,
                PREP_RUN,
                "AgentWorkflow",
                {
                    "workflow_run_id": RUN,
                    "workflow_phase": "supervisor_prepare",
                    "workflow_node_key": None,
                },
                dict(workflow_id=CHILD_ID, run_id=CHILD_RUN, initiated_event_id=35),
                status="COMPLETED",
            ),
        }
        self.terminated = []
        self.fail_read = False
        self.stuck = False

    def read(self, workflow_id, run_id=None):
        if self.fail_read:
            raise RuntimeError("private transport details must not escape")
        row = self.rows.get(workflow_id)
        if row is not None and run_id is not None:
            assert run_id == row["run_id"], "recovery must pin the exact execution"
        return copy.deepcopy(row)

    def terminate(self, workflow_id, run_id):
        assert run_id == self.rows[workflow_id]["run_id"]
        self.terminated.append((workflow_id, run_id))
        if not self.stuck:
            self.rows[workflow_id]["status"] = "TERMINATED"


def controller(h, tmp_path, db=None):
    db = db or Database()
    c = h.FixtureController(
        h.Config(),
        tmp_path / "private/state.json",
        MARKER,
        api=Api(db),
        database=db,
        runner_cleaner=Runner(),
    )
    # Set after construction so the pre-fix regression is an assertion failure,
    # not a missing constructor parameter.
    c.temporal_transport = TemporalTransport()
    h.save_state(c.state_path, state(h))
    return c


def test_default_temporal_graph_bound_rejects_more_than_32_valid_executions(h, tmp_path):
    c = controller(h, tmp_path)
    root = c.temporal_transport.rows[ROOT_ID]
    for number in range(30):
        turn = f"{number + 100:08x}-3333-4333-8333-333333333333"
        run = f"{number + 200:08x}-4444-4444-8444-444444444444"
        workflow_id = f"{CONVERSATION}-eval-{turn}"
        initiation = number + 100
        root["children"].append(
            dict(workflow_id=workflow_id, run_id=run, initiated_event_id=initiation)
        )
        c.temporal_transport.rows[workflow_id] = dict(
            workflow_id=workflow_id,
            run_id=run,
            workflow_type="EvaluationWorkflow",
            task_queue=f"session-{CONVERSATION}",
            input={"conversation_id": CONVERSATION, "turn_id": turn},
            parent=dict(
                workflow_id=ROOT_ID,
                run_id=ROOT_RUN,
                initiated_event_id=initiation,
            ),
            children=[],
            status="COMPLETED",
            continued=False,
            pending_children=False,
        )

    assert c.max_temporal_executions == 32
    owned_state = state(h)
    owned_state.workflow_run_ids = [RUN]
    with pytest.raises(h.FixtureError, match="unexpected execution graph"):
        c.temporal_tree(owned_state, CONVERSATION, root)


@pytest.mark.parametrize(
    "node", ["gamma", "delta", "epsilon", "zeta", "draft", "review", "foreign"]
)
def test_branching_cleanup_accepts_only_fixed_node_keys_with_exact_ancestry(h, tmp_path, node):
    c = controller(h, tmp_path)
    child_id = f"{RUN}:node:{node}"
    child = c.temporal_transport.rows.pop(PREP_ID)
    child.update(workflow_id=child_id, input={"workflow_run_id": RUN, "workflow_phase": "node", "workflow_node_key": node})
    c.temporal_transport.rows[child_id] = child
    c.temporal_transport.rows[CHILD_ID]["children"][0]["workflow_id"] = child_id
    if node == "foreign":
        with pytest.raises(h.FixtureError, match="Temporal"):
            c.cleanup()
        assert not c.database.deleted
        assert not c.temporal_transport.terminated
    else:
        c.cleanup()
        c.verify_clean()


@pytest.mark.parametrize("foreign_parent", [False, True])
def test_loop_repeated_child_id_pins_each_run_and_parent(h, tmp_path, foreign_parent):
    c = controller(h, tmp_path)
    rows = {(r["workflow_id"], r["run_id"]): r for r in c.temporal_transport.rows.values()}
    child_id = f"{RUN}:node:start"
    orchestrator = rows[(CHILD_ID, CHILD_RUN)]
    for number in range(3):
        run_id = f"{number + 10:08x}-2222-4222-8222-222222222222"
        initiation = 40 + number
        child = copy.deepcopy(rows[(PREP_ID, PREP_RUN)])
        child.update(workflow_id=child_id, run_id=run_id,
                     input={"workflow_run_id": RUN, "workflow_phase": "node", "workflow_node_key": "start"},
                     parent={"workflow_id": CHILD_ID, "run_id": CHILD_RUN, "initiated_event_id": initiation})
        if foreign_parent and number == 2:
            child["parent"]["run_id"] = ROOT_RUN
        rows[(child_id, run_id)] = child
        orchestrator["children"].append(dict(workflow_id=child_id, run_id=run_id, initiated_event_id=initiation))
    class Runs:
        def read(self, workflow_id, run_id=None):
            return copy.deepcopy(rows[(workflow_id, run_id or ROOT_RUN)])
        def terminate(self, workflow_id, run_id):
            rows[(workflow_id, run_id)]["status"] = "TERMINATED"
    c.temporal_transport = Runs()
    if foreign_parent:
        with pytest.raises(h.FixtureError, match="Temporal"):
            c.cleanup()
        assert not c.database.deleted
        assert rows[(ROOT_ID, ROOT_RUN)]["status"] == "RUNNING"
    else:
        c.cleanup()
        c.verify_clean()
        assert rows[(ROOT_ID, ROOT_RUN)]["status"] == "TERMINATED"
        assert len([key for key in rows if key[0] == child_id]) == 3


def test_temporal_closed_before_any_destructive_cleanup_and_history_preserved(h, tmp_path):
    c = controller(h, tmp_path)
    original = c.api.request

    def request(*args, **kwargs):
        if args[1] == "DELETE":
            assert c.temporal_transport.rows[ROOT_ID]["status"] == "TERMINATED"
            assert c.temporal_transport.rows[CHILD_ID]["status"] == "TERMINATED"
            assert h.load_state(c.state_path).temporal_root_run_ids == {CONVERSATION: ROOT_RUN}
        return original(*args, **kwargs)

    c.api.request = request
    c.cleanup()
    assert c.temporal_transport.terminated == [(ROOT_ID, ROOT_RUN), (CHILD_ID, CHILD_RUN)]
    assert c.temporal_transport.rows[PREP_ID]["status"] == "COMPLETED"
    assert len(c.temporal_transport.rows) == 3
    c.cleanup()
    c.verify_clean()
    assert len(c.temporal_transport.terminated) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        "root_input",
        "queue",
        "foreign_parent",
        "parent_run",
        "initiated_event",
        "foreign_run",
        "child_input",
        "continued",
        "missing_child",
    ],
)
def test_temporal_foreign_identity_blocks_all_mutations_and_retains_journal(h, tmp_path, mutation):
    c = controller(h, tmp_path)
    rows = c.temporal_transport.rows
    if mutation == "root_input":
        rows[ROOT_ID]["input"]["conversation_id"] = ADMIN
    elif mutation == "queue":
        rows[CHILD_ID]["task_queue"] = f"session-{ADMIN}"
    elif mutation == "foreign_parent":
        rows[CHILD_ID]["parent"]["workflow_id"] = f"conversation-{ADMIN}"
    elif mutation == "parent_run":
        rows[CHILD_ID]["parent"]["run_id"] = PREP_RUN
    elif mutation == "initiated_event":
        rows[CHILD_ID]["parent"]["initiated_event_id"] = 99
    elif mutation == "foreign_run":
        c.database.data["run"] = []
    elif mutation == "child_input":
        rows[PREP_ID]["input"]["workflow_run_id"] = ADMIN
    elif mutation == "continued":
        rows[CHILD_ID]["continued"] = True
    else:
        del rows[PREP_ID]
    with pytest.raises(h.FixtureError, match="Temporal"):
        c.cleanup()
    assert not c.database.deleted
    assert not c.api.deleted_conversations
    assert not c.runner_cleaner.removed
    assert not c.temporal_transport.terminated
    assert c.state_path.exists()
    assert c.database.data["capabilities"] == ["chat.send"]


@pytest.mark.parametrize("failure", ["unavailable", "not_closed"])
def test_temporal_closure_failure_keeps_metadata_runner_and_journal(h, tmp_path, failure):
    c = controller(h, tmp_path)
    c.temporal_transport.fail_read = failure == "unavailable"
    c.temporal_transport.stuck = failure == "not_closed"
    with pytest.raises(h.FixtureError, match="Temporal"):
        c.cleanup()
    assert not c.database.deleted and not c.runner_cleaner.removed
    assert c.state_path.exists()


def test_verify_clean_rejects_open_execution_even_after_legacy_database_cleanup(h, tmp_path):
    c = controller(h, tmp_path)
    s = h.load_state(c.state_path)
    c.discover(s, c.database.data)
    h.save_state(c.state_path, s)
    c.database.data = {k: [] for k in c.database.data}
    c.database.data["capabilities"] = ["chat.send"]
    c.runner_cleaner.removed = [CONVERSATION]
    with pytest.raises(h.FixtureError, match="Temporal"):
        c.verify_clean()
    assert not c.temporal_transport.terminated
    c.cleanup()
    c.verify_clean()
    c.temporal_transport.fail_read = True
    with pytest.raises(h.FixtureError, match="Temporal"):
        c.verify_clean()
    assert c.state_path.exists()


def test_temporal_reused_root_id_is_not_adopted_after_journal_pinning(h, tmp_path):
    c = controller(h, tmp_path)
    c.cleanup()
    c.temporal_transport.rows[ROOT_ID]["run_id"] = PREP_RUN
    with pytest.raises(h.FixtureError, match="Temporal"):
        c.cleanup()
    assert len(c.temporal_transport.terminated) == 2


def test_temporal_never_started_conversation_can_clean_but_missing_pinned_history_cannot(
    h, tmp_path
):
    c = controller(h, tmp_path)
    c.temporal_transport.rows = {}
    c.database.data["run"] = []
    c.cleanup()
    c.verify_clean()
    assert h.load_state(c.state_path).temporal_root_run_ids == {CONVERSATION: None}
    s = h.load_state(c.state_path)
    s.temporal_root_run_ids[CONVERSATION] = ROOT_RUN
    h.save_state(c.state_path, s)
    with pytest.raises(h.FixtureError, match="Temporal"):
        c.verify_clean()


def test_temporal_child_started_during_parent_closure_is_revalidated_and_closed(h, tmp_path):
    c = controller(h, tmp_path)
    c.temporal_transport.rows[CHILD_ID]["children"] = []
    original = c.temporal_transport.terminate

    def terminate(id, run):
        original(id, run)
        if id == ROOT_ID:
            c.temporal_transport.rows[CHILD_ID]["children"] = [
                dict(workflow_id=PREP_ID, run_id=PREP_RUN, initiated_event_id=35)
            ]
            c.temporal_transport.rows[PREP_ID]["status"] = "RUNNING"

    c.temporal_transport.terminate = terminate
    c.cleanup()
    c.verify_clean()
    assert c.temporal_transport.terminated == [
        (ROOT_ID, ROOT_RUN),
        (CHILD_ID, CHILD_RUN),
        (PREP_ID, PREP_RUN),
    ]


def test_temporal_late_foreign_child_blocks_deletion_after_root_closure(h, tmp_path):
    c = controller(h, tmp_path)
    original = c.temporal_transport.terminate

    def terminate(id, run):
        original(id, run)
        c.temporal_transport.rows[PREP_ID]["parent"]["run_id"] = ROOT_RUN

    c.temporal_transport.terminate = terminate
    with pytest.raises(h.FixtureError, match="Temporal"):
        c.cleanup()
    assert c.temporal_transport.terminated == [(ROOT_ID, ROOT_RUN)]
    assert not c.database.deleted and not c.runner_cleaner.removed


def test_temporal_never_started_conversation_alongside_proven_owned_run(h, tmp_path):
    c = controller(h, tmp_path)
    s = h.load_state(c.state_path)
    s.conversation_ids = [ADMIN]  # previously recorded, no turn was sent
    h.save_state(c.state_path, s)
    c.cleanup()
    c.verify_clean()
    assert h.load_state(c.state_path).temporal_root_run_ids[ADMIN] is None


def test_temporal_unresolved_child_start_blocks_destructive_cleanup(h, tmp_path):
    c = controller(h, tmp_path)
    c.temporal_transport.rows[ROOT_ID]["pending_children"] = True
    with pytest.raises(h.FixtureError, match="Temporal"):
        c.cleanup()
    assert not c.database.deleted and not c.temporal_transport.terminated


@pytest.mark.parametrize("mutation", [None, "turn", "conversation", "parent", "children"])
def test_temporal_evaluator_requires_exact_conversation_turn_and_direct_parent(
    h, tmp_path, mutation
):
    c = controller(h, tmp_path)
    evaluator_id = f"{CONVERSATION}-eval-{RUN}"
    evaluator = copy.deepcopy(c.temporal_transport.rows[PREP_ID])
    evaluator.update(
        workflow_id=evaluator_id,
        run_id=PREP_RUN,
        workflow_type="EvaluationWorkflow",
        input={"conversation_id": CONVERSATION, "turn_id": RUN},
        parent=dict(workflow_id=ROOT_ID, run_id=ROOT_RUN, initiated_event_id=39),
    )
    c.temporal_transport.rows[evaluator_id] = evaluator
    c.temporal_transport.rows[ROOT_ID]["children"].append(
        dict(workflow_id=evaluator_id, run_id=PREP_RUN, initiated_event_id=39)
    )
    if mutation == "turn":
        evaluator["input"]["turn_id"] = ADMIN
    elif mutation == "conversation":
        evaluator["input"]["conversation_id"] = ADMIN
    elif mutation == "parent":
        evaluator["parent"]["workflow_id"] = CHILD_ID
    elif mutation == "children":
        evaluator["children"] = [dict(workflow_id=PREP_ID, run_id=PREP_RUN, initiated_event_id=35)]
    if mutation:
        with pytest.raises(h.FixtureError, match="Temporal"):
            c.cleanup()
        assert not c.temporal_transport.terminated and not c.database.deleted
    else:
        c.cleanup()
        c.verify_clean()
        assert evaluator["status"] == "COMPLETED"
        assert c.temporal_transport.terminated == [(ROOT_ID, ROOT_RUN), (CHILD_ID, CHILD_RUN)]


def test_temporal_transport_checks_compose_owner_and_passes_only_exact_handle(h, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[1] == "inspect":
            service = "postgres" if args[2] == "aurum-ai-postgres" else "backend"
            return subprocess.CompletedProcess(args, 0, f"owned-project {service}\n", "")
        return subprocess.CompletedProcess(args, 0, "null", "")

    monkeypatch.setattr(h.subprocess, "run", run)
    transport = h.TemporalTransport(h.Config())
    transport.terminate(ROOT_ID, ROOT_RUN)
    args, kwargs = calls[-1]
    assert args[:5] == ["docker", "exec", "-i", "aurum-ai-backend", "/app/.venv/bin/python"]
    assert json.loads(kwargs["input"]) == {
        "action": "terminate",
        "workflow_id": ROOT_ID,
        "run_id": ROOT_RUN,
    }
    assert len(calls) == 3

    def foreign(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "foreign backend", "")

    monkeypatch.setattr(h.subprocess, "run", foreign)
    with pytest.raises(h.FixtureError, match="ownership"):
        h.TemporalTransport(h.Config()).terminate(ROOT_ID, ROOT_RUN)


def _owned_inspect(args):
    service = "postgres" if args[2] == "aurum-ai-postgres" else "backend"
    return subprocess.CompletedProcess(args, 0, f"owned-project {service}\n", "")


def test_temporal_transport_retries_transient_rpc_failures(h, monkeypatch):
    # A freshly recreated backend container can fail one docker exec/RPC while
    # the journal is intact; read/terminate are idempotent, so retry is safe.
    outcomes = [
        subprocess.CompletedProcess([], 1, "", "Temporal RPC unavailable (RPCError/UNAVAILABLE)\n"),
        subprocess.CompletedProcess([], 125, "", "Error response from daemon\n"),
        subprocess.CompletedProcess([], 0, "null", ""),
    ]
    sleeps = []

    def run(args, **kwargs):
        if args[1] == "inspect":
            return _owned_inspect(args)
        return outcomes.pop(0)

    monkeypatch.setattr(h.subprocess, "run", run)
    monkeypatch.setattr(h.time, "sleep", sleeps.append)
    assert h.TemporalTransport(h.Config()).read(ROOT_ID, ROOT_RUN) is None
    assert outcomes == [] and len(sleeps) == 2


def test_temporal_transport_reports_failure_kind_after_bounded_retries(h, monkeypatch):
    attempts = []

    def run(args, **kwargs):
        if args[1] == "inspect":
            return _owned_inspect(args)
        attempts.append(args)
        return subprocess.CompletedProcess(
            args, 1, "", "Temporal RPC unavailable (RPCError/UNAVAILABLE)\nprivate-secret\n"
        )

    monkeypatch.setattr(h.subprocess, "run", run)
    monkeypatch.setattr(h.time, "sleep", lambda _seconds: None)
    with pytest.raises(h.FixtureError) as error:
        h.TemporalTransport(h.Config()).read(ROOT_ID, ROOT_RUN)
    assert str(error.value) == "Temporal RPC unavailable (RPCError/UNAVAILABLE); journal retained"
    assert len(attempts) == h.TemporalTransport.attempts


def test_temporal_transport_retries_exec_timeouts(h, monkeypatch):
    calls = []

    def run(args, **kwargs):
        if args[1] == "inspect":
            return _owned_inspect(args)
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return subprocess.CompletedProcess(args, 0, "null", "")

    monkeypatch.setattr(h.subprocess, "run", run)
    monkeypatch.setattr(h.time, "sleep", lambda _seconds: None)
    assert h.TemporalTransport(h.Config()).read(ROOT_ID, ROOT_RUN) is None
    assert len(calls) == 2


def test_temporal_rpc_depends_only_on_explicit_temporal_environment(h):
    assert "from src.core.config import settings" not in h.TEMPORAL_RPC
    assert 'os.environ["APP_TEMPORAL_HOST"]' in h.TEMPORAL_RPC
    assert 'os.environ["APP_TEMPORAL_NAMESPACE"]' in h.TEMPORAL_RPC


@pytest.mark.parametrize("action", ["read", "terminate", "unavailable"])
def test_temporal_sdk_projection_and_exact_run_without_payload_leak(h, monkeypatch, capsys, action):
    import io
    from types import SimpleNamespace
    from temporalio.api.history.v1 import HistoryEvent, WorkflowExecutionStartedEventAttributes
    from temporalio.api.common.v1 import Payload, Payloads

    import temporalio.client

    calls = []
    start = WorkflowExecutionStartedEventAttributes(
        input=Payloads(
            payloads=[
                Payload(
                    metadata={"encoding": b"json/plain"},
                    data=json.dumps(
                        {"conversation_id": CONVERSATION, "session_token": "private-secret"}
                    ).encode(),
                )
            ]
        )
    )

    class Handle:
        async def describe(self):
            if action == "unavailable":
                raise RuntimeError("private-secret")
            return SimpleNamespace(
                id=ROOT_ID,
                run_id=ROOT_RUN,
                workflow_type="ConversationWorkflow",
                task_queue=f"session-{CONVERSATION}",
                status=SimpleNamespace(name="RUNNING"),
            )

        async def fetch_history(self):
            return SimpleNamespace(
                events=[HistoryEvent(workflow_execution_started_event_attributes=start)]
            )

        async def terminate(self, **kwargs):
            calls.append("terminated")

    class Client:
        from temporalio.converter import DataConverter

        data_converter = DataConverter.default

        @staticmethod
        async def connect(*args, **kwargs):
            return Client()

        def get_workflow_handle(self, id, *, run_id):
            calls.append((id, run_id))
            return Handle()

    monkeypatch.setattr(temporalio.client, "Client", Client)
    monkeypatch.setenv("APP_TEMPORAL_HOST", "fixture:7233")
    monkeypatch.setenv("APP_TEMPORAL_NAMESPACE", "default")
    monkeypatch.setitem(
        sys.modules,
        "src.core.config",
        SimpleNamespace(
            settings=SimpleNamespace(TEMPORAL_HOST="fixture:7233", TEMPORAL_NAMESPACE="default")
        ),
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "action": "read" if action == "unavailable" else action,
                    "workflow_id": ROOT_ID,
                    "run_id": ROOT_RUN,
                }
            )
        ),
    )
    if action == "unavailable":
        with pytest.raises(SystemExit) as exit_info:
            exec(h.TEMPORAL_RPC, {})
        assert exit_info.value.code == 1
        captured = capsys.readouterr()
        # Only the exception class is reported; its message may carry payloads.
        assert captured.err == "Temporal RPC unavailable (RuntimeError)\n"
        assert "private-secret" not in captured.out + captured.err
        return
    else:
        exec(h.TEMPORAL_RPC, {})
    captured = capsys.readouterr()
    assert "private-secret" not in captured.out + captured.err
    assert calls[0] == (ROOT_ID, ROOT_RUN)
    if action == "read":
        assert json.loads(captured.out)["input"] == {"conversation_id": CONVERSATION}
        assert "terminated" not in calls
    elif action == "terminate":
        assert calls == [(ROOT_ID, ROOT_RUN), (ROOT_ID, ROOT_RUN), "terminated"]


def test_unrecorded_instantiation_is_recovered_graph_owned_and_idempotent(h, tmp_path):
    c = controller(h, tmp_path)
    c.cleanup()
    recovered = h.load_state(c.state_path)
    assert recovered.workflow_ids == [COPY]
    assert recovered.agent_ids == [CLONE]
    assert recovered.source_agent_ids == {"v1": A1, "v2": A2}
    assert recovered.conversation_ids == [CONVERSATION]
    assert recovered.workflow_run_ids == [RUN]
    assert c.runner_cleaner.removed == [CONVERSATION]
    c.cleanup()
    c.verify_clean()
    assert c.state_path.exists(), "wrapper owns journal removal after service readiness"


def test_source_workflow_run_cleanup_keeps_definition_until_template_removed(h, tmp_path):
    c = controller(h, tmp_path)
    c.database.data["run"][0]["workflow_definition_id"] = SOURCE
    original_delete = c.database.delete_owned

    def check_references(state, owned):
        if owned["run"]:
            assert SOURCE not in {r["id"] for r in owned["workflow"]}
            assert SOURCE in {r["id"] for r in owned.get("run_definition", [])}
            assert TEMPLATE in {r["id"] for r in c.database.data["template"]}
        original_delete(state, owned)

    c.database.delete_owned = check_references
    c.cleanup()
    c.verify_clean()


def test_conversation_cleanup_uses_registered_member_route_before_database_delete(h, tmp_path):
    c = controller(h, tmp_path)
    original_delete = c.database.delete_owned

    def verify_before_delete(state, owned):
        if owned["conversation"]:
            assert c.api.deleted_conversations == [
                ("business", f"/v1/conversations/{CONVERSATION}", "business-ui:user")
            ]
            assert c.runner_cleaner.removed == [CONVERSATION]
        original_delete(state, owned)

    c.database.delete_owned = verify_before_delete
    c.cleanup()
    assert c.database.data["conversation"] == []


@pytest.mark.parametrize(
    "mutation", ["org", "author", "provenance", "foreign_agent", "foreign_copy", "foreign_run"]
)
def test_foreign_ownership_or_reference_blocks_all_deletes_but_restores(h, tmp_path, mutation):
    db = Database()
    if mutation == "org":
        db.data["workflow"][1]["org_id"] = ADMIN
    if mutation == "author":
        db.data["agent"][2]["created_by"] = MEMBER
    if mutation == "provenance":
        db.data["workflow"][1]["source_template_id"] = SOURCE
    if mutation == "foreign_agent":
        db.data["workflow"].append(
            row(MEMBER, "foreign", agent_ids=[CLONE], source_template_id=None)
        )
    if mutation == "foreign_copy":
        db.data["workflow"].append(
            row(MEMBER, "foreign", agent_ids=[], source_template_id=TEMPLATE)
        )
    if mutation == "foreign_run":
        db.data["run"][0]["initiating_user_id"] = ADMIN
    db.data["capabilities"] = ["chat.send", "workspace.agents.create"]
    c = controller(h, tmp_path, db)
    with pytest.raises(h.FixtureError):
        c.cleanup()
    assert db.deleted == []
    assert not any(call[1] == "DELETE" for call in c.api.calls)
    assert db.data["capabilities"] == ["chat.send"]
    assert c.state_path.exists()


def test_record_validates_uuid_and_server_graph_not_browser_uuid(h, tmp_path):
    c = controller(h, tmp_path)
    for id in ("not-uuid", MEMBER):
        with pytest.raises(h.FixtureError):
            c.record("agent", id)
    c.record("agent", CLONE)
    c.record("agent", A2)
    assert h.load_state(c.state_path).agent_ids == [CLONE]


def test_cleanup_failure_still_restores_and_verification_catches_mismatch(h, tmp_path):
    db = Database()
    db.fail = True
    c = controller(h, tmp_path, db)
    with pytest.raises(h.FixtureError):
        c.cleanup()
    assert any(call[2].endswith("capabilities/organization") for call in c.api.calls)
    with pytest.raises(h.FixtureError):
        c.verify_clean()
    db.fail = False
    c.cleanup()
    db.data["capabilities"].append("workspace.agents.create")
    with pytest.raises(h.FixtureError, match="restor"):
        c.verify_clean()


def test_partial_copy_agent_delete_retry_uses_durable_exact_graph_proof(h, tmp_path):
    c = controller(h, tmp_path)
    original = c.api.request

    def fail_clone(plane, method, path, *args, **kwargs):
        if method == "DELETE" and path.endswith(CLONE):
            raise h.FixtureError("timeout after workflow deletion")
        return original(plane, method, path, *args, **kwargs)

    c.api.request = fail_clone
    with pytest.raises(h.FixtureError):
        c.cleanup()
    saved = h.load_state(c.state_path)
    assert saved.agent_proofs[CLONE] == {
        "name": "x" * 216 + f" [{CLONE}]",
        "workflow_id": COPY,
        "template_id": TEMPLATE,
        "template_version": 1,
    }
    assert COPY not in {r["id"] for r in c.database.data["workflow"]}
    assert TEMPLATE in {r["id"] for r in c.database.data["template"]}
    c.api.request = original
    c.cleanup()
    c.verify_clean()


def test_foreign_historical_node_agent_reference_blocks_before_mutation(h, tmp_path):
    db = Database()
    db.data["run"].append(
        {
            "id": MEMBER,
            "org_id": ORG,
            "initiating_user_id": MEMBER,
            "conversation_id": ADMIN,
            "workflow_definition_id": ADMIN,
            "agent_ids": [CLONE],
        }
    )
    c = controller(h, tmp_path, db)
    with pytest.raises(h.FixtureError):
        c.cleanup()
    assert db.deleted == []


def test_conversation_attachment_blocks_metadata_cascade_without_fallback(h, tmp_path):
    db = Database()
    db.data["conversation"][0]["has_attachments"] = True
    c = controller(h, tmp_path, db)
    with pytest.raises(h.FixtureError):
        c.cleanup()
    assert db.deleted == []
    assert not any(call[1] == "DELETE" for call in c.api.calls)


def test_database_boundary_uses_quoted_parameters_and_never_leaks_stderr(h, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs["input"]))
        return subprocess.CompletedProcess(command, 1, "", "database-secret")

    monkeypatch.setattr(h.subprocess, "run", run)
    with pytest.raises(h.FixtureError) as caught:
        h.Database(h.Config()).delete_owned(state(h), inventory())
    assert "database-secret" not in str(caught.value)
    command, sql = calls[0]
    assert "org=" + ORG in command
    assert any(v.startswith("owned=") and MARKER in v for v in command)
    assert MARKER not in sql and ORG not in sql
    assert "w.source_template_id IS NOT DISTINCT FROM f.provenance" in sql
    assert "w.name=f.name" in sql and "w.created_by=f.owner_id" in sql
    assert "INSERT INTO fixture_owned SELECT 'run_definition'" in sql
    assert "kind IN ('workflow', 'run_definition')" in sql
    definition_delete = next(s for s in sql.split(";") if "DELETE FROM workflow_definitions" in s)
    assert "f.kind='workflow'" in definition_delete
    assert "run_definition" not in definition_delete


def test_database_cleanup_deletes_only_exact_owned_turns_before_runs_and_fails_closed(
    h, monkeypatch
):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(h.subprocess, "run", run)
    h.Database(h.Config()).delete_owned(state(h), inventory())

    _, sql = calls[0]
    turn_delete = sql.index("DELETE FROM interaction_turns")
    run_delete = sql.index("DELETE FROM workflow_runs")
    assert turn_delete < run_delete
    assert "t.org_id=:'org'::uuid" in sql
    assert "t.conversation_id=c.id" in sql
    assert "t.owner_id=c.owner_id" in sql
    assert "owned_c.user_id=c.owner_id" in sql
    assert "owned_c.title=c.name" in sql
    assert "k.owning_conversation_id=owned_c.id" in sql
    assert "t.workflow_run_id IS NULL" in sql
    assert "r.id=t.workflow_run_id" in sql
    assert "r.initiating_user_id=f.owner_id" in sql
    assert "r.workflow_definition_id IN (SELECT id FROM fixture_owned WHERE kind IN ('workflow', 'run_definition'))" in sql

    run_statement = next(s for s in sql.split(";") if "DELETE FROM workflow_runs" in s)
    assert "owned_c.id=c.id" in run_statement
    assert "owned_c.user_id=c.owner_id" in run_statement
    assert "owned_c.title=c.name" in run_statement
    assert "r.initiating_user_id=f.owner_id" in run_statement
    assert "f.owner_id=c.owner_id" in run_statement

    assertion = sql.index("owned cleanup target remains")
    assert run_delete < assertion < sql.rindex("COMMIT")
    for table in (
        "conversations",
        "workflow_runs",
        "workflow_definitions",
        "workflow_templates",
    ):
        assert f"SELECT 1 FROM {table}" in sql[assertion - 1000 :]


def test_recovery_cli_derives_model_without_wrapper_environment(h, tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_GATEWAY_MODEL_ID", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(HARNESS), "cleanup", "--state", str(tmp_path / "missing.json"), "--marker", MARKER],
    )
    assert h.main() == 0


def test_recovery_after_overlay_disappears_removes_only_owned_model_grants(h, tmp_path):
    c = controller(h, tmp_path)
    c.database.data["model_grants"] = [
        {"principalType": "user", "principalId": MEMBER, "permission": "read"}
    ]
    original = c.api.request

    def missing_model(plane, method, path, *args, **kwargs):
        if path.endswith("grants/reconcile"):
            return {"_httpStatus": 404}
        return original(plane, method, path, *args, **kwargs)

    c.api.request = missing_model
    c.cleanup()
    c.verify_clean()
    assert c.database.data["model_grants"] == []


@pytest.mark.parametrize("timeout", [False, True])
def test_prepare_only_source_mutations_persist_intent_and_recover_timeout(h, tmp_path, timeout):
    data = {
        "agent": [],
        "workflow": [],
        "template": [],
        "conversation": [],
        "run": [],
        "capabilities": ["chat.send"],
        "model_grants": [],
    }
    db = Database(data)
    path = tmp_path / "private/state.json"

    class PrepareApi(Api):
        def request(self, plane, method, route, token, *, body=None, expected=(200,)):
            if route == "/v1/auth/me":
                return {
                    "data": {"user_id": MEMBER if token.endswith(":user") else ADMIN, "org_id": ORG}
                }
            if method == "POST":
                saved = h.load_state(path)
                assert saved.organization_id == ORG
                assert saved.original_capabilities == ["chat.send"]
                assert token == "admin-ui:admin"
                self.calls.append((plane, method, route, body))
                if route == "/v1/agent-definitions":
                    id = A1 if not db.data["agent"] else A2
                    db.data["agent"].append(row(id, body["name"]))
                    if timeout:
                        raise h.FixtureError("HTTP transport failed; outcome unknown")
                elif route == "/v1/admin/workflows":
                    id = SOURCE
                    db.data["workflow"].append(
                        row(
                            SOURCE,
                            body["name"],
                            source_template_id=None,
                            source_template_version=None,
                            agent_ids=[A1],
                        )
                    )
                else:
                    raise AssertionError("prepare must never create template or copy")
                return {"data": {"id": id}}
            return super().request(plane, method, route, token, body=body, expected=expected)

    api = PrepareApi(db)
    c = h.FixtureController(h.Config(), path, MARKER, api=api, database=db, runner_cleaner=Runner())
    if timeout:
        with pytest.raises(h.FixtureError):
            c.prepare()
        assert db.data["agent"][0]["id"] == A1
        c.cleanup()
        c.verify_clean()
        assert h.load_state(path).source_agent_ids == {"v1": A1}
    else:
        c.prepare()
        saved = h.load_state(path)
        assert saved.source_agent_ids == {"v1": A1, "v2": A2}
        assert saved.source_workflow_id == SOURCE
        creates = [call for call in api.calls if call[1] == "POST"]
        assert len(creates) == 3
        a, b = creates[0][3], creates[1][3]
        assert a["systemPrompt"] != b["systemPrompt"] and a["temperature"] != b["temperature"]
        graph = creates[2][3]
        assert graph["supervisorAgentDefinitionId"] == A1
        assert [n["agentDefinitionId"] for n in graph["nodes"]] == [A1, A1]
        assert [n["requiresConfirmation"] for n in graph["nodes"]] == [True, False]
        assert graph["edges"] == [
            {"sourceNodeKey": "alpha", "targetNodeKey": "beta", "condition": "on_success"}
        ]
        with pytest.raises(h.FixtureError):
            c.prepare()


def fake_executable(directory, name, body):
    path = directory / name
    path.write_text("#!/usr/bin/env bash\nset -eu\n" + body)
    path.chmod(0o755)


@pytest.mark.parametrize(
    "browser,cleanup,restore,verify,ready,want",
    [
        (0, 0, 0, 0, 0, 0),
        (7, 9, 11, 0, 0, 7),
        (7, 0, 0, 0, 0, 7),
        (0, 9, 0, 0, 0, 9),
        (0, 0, 11, 0, 0, 11),
        (0, 0, 0, 12, 0, 12),
        (0, 0, 0, 0, 1, 1),
    ],
)
@pytest.mark.parametrize("acceptance", ["templates", "routing", "branching", "hitl", "loops"])
def test_real_wrapper_exit_restoration_and_journal(
    tmp_path, browser, cleanup, restore, verify, ready, want, acceptance
):
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
    printf '%s\\n' "$name"
  else printf 'aurum-ai\\n'; fi
fi
if [[ "$*" == *"--force-recreate backend backend-admin"* && "$*" != *docker-compose.e2e.yml* ]]; then touch "$RESTORED_FLAG"; exit "$RESTORE"; fi
""",
    )
    fake_executable(
        fake_bin, "curl", prefix + 'if [[ -f "$RESTORED_FLAG" ]]; then exit "$READY"; fi\n'
    )
    fake_executable(fake_bin, "sleep", "exit 0\n")
    fake_executable(fake_bin, "npm", prefix + 'exit "$BROWSER"\n')
    fake_executable(
        fake_bin,
        "python3",
        prefix
        + """
if [[ "${1:-}" == -c ]]; then printf '0123456789abcdef\\n'; exit 0; fi
if [[ "$1" == *workflow_loops_e2e.py ]]; then exit "$BROWSER"; fi
command="$2"; shift 2
while (( $# )); do case "$1" in --state) state="$2"; shift 2;; *) shift;; esac; done
if [[ "$command" == prepare ]]; then printf '{}\\n' > "$state"; fi
if [[ "$command" == cleanup ]]; then exit "$CLEANUP"; fi
if [[ "$command" == verify-clean ]]; then exit "$VERIFY"; fi
""",
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "APP_SECRET_KEY": "c5-offline-test-key-0123456789abcdef",
        "COMMAND_LOG": str(log),
        "BROWSER": str(browser),
        "CLEANUP": str(cleanup),
        "RESTORE": str(restore),
        "VERIFY": str(verify),
        "READY": str(ready),
        "RESTORED_FLAG": str(tmp_path / "restored"),
        "WORKFLOW_ACCEPTANCE_KIND": "templates",
    }
    result = subprocess.run(
        ["bash", str(ROOT / ("smoke-test-workflow-loops.sh" if acceptance == "loops" else f"smoke-test-workflow-{acceptance}-ui.sh"))],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == want, result.stderr
    commands = log.read_text().splitlines()
    prepare = next(c for c in commands if " prepare " in c)
    journal = Path(prepare.split("--state ")[1].split(" --marker")[0])
    assert journal.exists() == bool(want)
    assert any(
        "--force-recreate backend backend-admin" in c and "docker-compose.e2e.yml" not in c
        for c in commands
    )
    assert any(" verify-clean " in c for c in commands)
    if acceptance == "loops":
        assert any("workflow_loops_e2e.py" in c for c in commands)
        assert not any(c.startswith("npm ") for c in commands)
    else:
        assert any("--workers=1 --trace=off" in c for c in commands)
        assert any(f"e2e/workflow-{acceptance}.real.spec.ts" in c for c in commands)
    if acceptance == "hitl":
        assert any("docker-compose.e2e-hitl.yml" in c and "build hitl-runner" in c for c in commands)
        assert any("docker-compose.e2e-hitl.yml" in c and "--force-recreate backend backend-admin test-gateway" in c for c in commands)
        assert commands.index(next(c for c in commands if "build runner" in c)) < commands.index(next(c for c in commands if "build hitl-runner" in c))
    else:
        assert not any("docker-compose.e2e-hitl.yml" in c for c in commands)
    if want:
        assert "recovery state retained" in result.stderr
        assert "E2E_POSTGRES_CONTAINER=" in result.stderr


def test_temporal_transport_uses_explicit_owned_backend_container(h, monkeypatch):
    calls = []
    monkeypatch.setenv("E2E_BACKEND_CONTAINER", "owned-release-backend")
    monkeypatch.setenv("E2E_POSTGRES_CONTAINER", "owned-release-postgres")

    def run(args, **kwargs):
        calls.append(args)
        if args[1] == "inspect":
            service = "postgres" if args[2] == "owned-release-postgres" else "backend"
            return subprocess.CompletedProcess(args, 0, f"owned-project {service}\n", "")
        return subprocess.CompletedProcess(args, 0, "null", "")

    monkeypatch.setattr(h.subprocess, "run", run)
    h.TemporalTransport(h.Config()).terminate(ROOT_ID, ROOT_RUN)
    assert calls[1][2] == "owned-release-backend"
    assert calls[2][3] == "owned-release-backend"

    def foreign(args, **kwargs):
        service = "postgres" if args[2] == "owned-release-postgres" else "backend"
        project = "owned-project" if service == "postgres" else "foreign-project"
        return subprocess.CompletedProcess(args, 0, f"{project} {service}\n", "")

    monkeypatch.setattr(h.subprocess, "run", foreign)
    with pytest.raises(h.FixtureError, match="ownership"):
        h.TemporalTransport(h.Config()).terminate(ROOT_ID, ROOT_RUN)
