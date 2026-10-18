#!/usr/bin/env python3
"""Private exact-owned fixture lifecycle for durable user-question acceptance."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field, fields
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from workflow_templates_ui_e2e import (  # noqa: E402
    ApiClient,
    Config,
    Database as SharedDatabase,
    DockerRunnerCleaner,
    TemporalTransport,
)


MARKER = re.compile(r"user-questions-e2e-[a-f0-9]{16}")
QUESTION_TEXTS = {"Which city?", "Which district?", "Which loop value?", "Choose city?"}


class FixtureError(RuntimeError):
    """Operator-safe error that never contains credentials or response bodies."""


@dataclass
class FixtureState:
    marker: str
    organization_id: str = ""
    admin_id: str = ""
    member_id: str = ""
    original_model_grants: list[dict[str, Any]] | None = None
    conversation_ids: list[str] = field(default_factory=list)
    turn_ids: list[str] = field(default_factory=list)
    execution_ids: list[str] = field(default_factory=list)
    operation_ids: list[str] = field(default_factory=list)
    question_ids: list[str] = field(default_factory=list)
    outbox_ids: list[str] = field(default_factory=list)
    temporal_runs: dict[str, str] = field(default_factory=dict)
    runner_resources: dict[str, dict[str, str]] = field(default_factory=dict)
    prepared: bool = False


def canonical(value: Any) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise FixtureError("invalid canonical UUID") from exc
    return value


def validate_state(state: FixtureState) -> None:
    if not isinstance(state.marker, str) or not MARKER.fullmatch(state.marker):
        raise FixtureError("invalid ownership marker")
    for key in ("organization_id", "admin_id", "member_id"):
        value = getattr(state, key)
        if not isinstance(value, str):
            raise FixtureError("invalid journal schema")
        if value:
            canonical(value)
    for key in (
        "conversation_ids",
        "turn_ids",
        "execution_ids",
        "operation_ids",
        "question_ids",
        "outbox_ids",
    ):
        values = getattr(state, key)
        if not isinstance(values, list) or len(values) != len(set(values)):
            raise FixtureError("invalid resource journal schema")
        for value in values:
            canonical(value)
    if not isinstance(state.temporal_runs, dict):
        raise FixtureError("invalid Temporal journal schema")
    for workflow_id, run_id in state.temporal_runs.items():
        if not isinstance(workflow_id, str) or not workflow_id.startswith("conversation-"):
            raise FixtureError("invalid Temporal workflow identity")
        canonical(workflow_id.removeprefix("conversation-"))
        canonical(run_id)
    if not isinstance(state.runner_resources, dict):
        raise FixtureError("invalid runner resource journal schema")
    for conversation_id, resource in state.runner_resources.items():
        canonical(conversation_id)
        if not isinstance(resource, dict) or set(resource) != {
            "kind",
            "name",
            "namespace",
            "release",
            "uid",
        }:
            raise FixtureError("invalid runner resource journal schema")
        if resource["kind"] != "kubernetes":
            raise FixtureError("invalid runner resource journal schema")
        if resource["name"] != f"runner-{conversation_id}":
            raise FixtureError("invalid runner resource journal schema")
        if not all(
            isinstance(resource[key], str) and resource[key]
            for key in ("namespace", "release")
        ):
            raise FixtureError("invalid runner resource journal schema")
        canonical(resource["uid"])
    if state.original_model_grants is not None:
        if not isinstance(state.original_model_grants, list):
            raise FixtureError("invalid grant journal schema")
        for grant in state.original_model_grants:
            if not isinstance(grant, dict) or set(grant) != {
                "principalType",
                "principalId",
                "permission",
            }:
                raise FixtureError("invalid grant journal schema")
            canonical(grant["principalId"])
            if grant["principalType"] not in {"organization", "user", "group"}:
                raise FixtureError("invalid grant journal schema")
            if grant["permission"] not in {"read", "write"}:
                raise FixtureError("invalid grant journal schema")
    if not isinstance(state.prepared, bool):
        raise FixtureError("invalid journal schema")
    has_owned = any(
        getattr(state, key)
        for key in (
            "conversation_ids",
            "turn_ids",
            "execution_ids",
            "operation_ids",
            "question_ids",
            "outbox_ids",
        )
    )
    if (has_owned or state.original_model_grants is not None) and not all(
        (state.organization_id, state.admin_id, state.member_id)
    ):
        raise FixtureError("missing ownership identity")


def private_parent(path: Path) -> None:
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700, parents=False)
    parent = path.parent.stat()
    if (
        path.parent.is_symlink()
        or stat.S_IMODE(parent.st_mode) != 0o700
        or parent.st_uid != os.getuid()
        or path.is_symlink()
    ):
        raise FixtureError("journal requires an owned private mode700 parent")


def save_state(path: Path, state: FixtureState) -> None:
    validate_state(state)
    private_parent(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(asdict(state), stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_state(path: Path) -> FixtureState:
    private_parent(path)
    metadata = path.stat()
    if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.getuid():
        raise FixtureError("journal requires private mode600")
    try:
        raw = json.loads(path.read_text())
        expected = {item.name for item in fields(FixtureState)}
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ValueError
        state = FixtureState(**raw)
    except (OSError, TypeError, ValueError) as exc:
        raise FixtureError("invalid journal schema") from exc
    validate_state(state)
    return state


class Database(SharedDatabase):
    def inventory(self, org_id: str, model_id: str, marker: str | None = None) -> dict[str, Any]:
        del marker
        return self.query(
            """
SELECT json_build_object(
'conversation', (SELECT coalesce(json_agg(t),'[]') FROM (
  SELECT c.id, c.org_id, c.user_id, c.title, c.status,
    EXISTS(SELECT 1 FROM knowledge_bases k WHERE k.owning_conversation_id=c.id) AS has_attachments,
    EXISTS(SELECT 1 FROM workflow_runs r WHERE r.conversation_id=c.id) AS has_workflow_runs
  FROM conversations c WHERE c.org_id=:'org'::uuid) t),
'turn', (SELECT coalesce(json_agg(t),'[]') FROM (
  SELECT i.id, i.org_id, i.conversation_id, i.owner_id, i.status
  FROM interaction_turns i WHERE i.org_id=:'org'::uuid) t),
'execution', (SELECT coalesce(json_agg(t),'[]') FROM (
  SELECT e.id, e.org_id, e.turn_id, e.workflow_id, e.run_id, e.task_queue, e.phase,
    e.node_key, e.agent_definition_id, e.iteration
  FROM interaction_executions e WHERE e.org_id=:'org'::uuid) t),
'operation', (SELECT coalesce(json_agg(t),'[]') FROM (
  SELECT o.id, o.org_id, o.turn_id, o.execution_id, o.kind FROM tool_operations o
  WHERE o.org_id=:'org'::uuid) t),
'question', (SELECT coalesce(json_agg(t),'[]') FROM (
  SELECT q.id, q.org_id, q.turn_id, q.execution_id, q.operation_id, q.text, q.status
  FROM user_questions q WHERE q.org_id=:'org'::uuid) t),
'outbox', (SELECT coalesce(json_agg(t),'[]') FROM (
  SELECT o.id, o.org_id, o.turn_id, o.execution_id, o.question_id FROM interaction_outbox o
  WHERE o.org_id=:'org'::uuid) t),
'model_grants', (SELECT coalesce(json_agg(t),'[]') FROM (
  SELECT principal_type AS "principalType", principal_id AS "principalId", permission
  FROM access_grants WHERE org_id=:'org'::uuid AND resource_type='model' AND resource_id=:'model') t)
);
            """,
            org=canonical(org_id),
            model=model_id,
        )

    def delete_conversation(
        self, state: FixtureState, row: dict[str, Any]
    ) -> None:
        result = self.query(
            """
WITH deleted AS (
  DELETE FROM conversations c
  WHERE c.id=:'conversation'::uuid AND c.org_id=:'org'::uuid
    AND c.user_id=:'member'::uuid AND c.title=:'title' AND c.status='deleted'
    AND NOT EXISTS (SELECT 1 FROM knowledge_bases k WHERE k.owning_conversation_id=c.id)
    AND NOT EXISTS (SELECT 1 FROM workflow_runs r WHERE r.conversation_id=c.id)
  RETURNING c.id
)
SELECT json_build_object('deleted', (SELECT count(*) FROM deleted));
""",
            conversation=canonical(row["id"]),
            org=canonical(state.organization_id),
            member=canonical(state.member_id),
            title=row["title"],
        )
        if result != {"deleted": 1}:
            raise FixtureError("exact owned conversation deletion failed")

    def restore_model_grants(self, state: FixtureState, model_id: str) -> None:
        if state.original_model_grants != [] or model_id != f"{state.marker}-model":
            raise FixtureError("model grant fallback ownership mismatch")
        self.query(
            """DELETE FROM access_grants WHERE org_id=:'org'::uuid
AND resource_type='model' AND resource_id=:'model';""",
            org=canonical(state.organization_id),
            model=model_id,
        )

    def defer_delivery(
        self, state: FixtureState, question_id: str, seconds: int
    ) -> None:
        result = self.query(
            """
WITH target AS (
  SELECT o.id FROM interaction_outbox o
  JOIN user_questions q ON q.org_id=o.org_id AND q.turn_id=o.turn_id
    AND q.execution_id=o.execution_id AND q.id=o.question_id
  WHERE o.org_id=:'org'::uuid AND o.question_id=:'question'::uuid
    AND o.event_kind='question_available' AND q.status='answered'
    AND o.question_version=q.version
), updated AS (
  UPDATE interaction_outbox o SET status='pending', attempts=o.attempts+1,
    available_at=clock_timestamp()+make_interval(secs => :'seconds'::integer),
    lease_id=NULL, lease_until=NULL, delivered_at=NULL, error_code=NULL,
    updated_at=clock_timestamp()
  FROM target t WHERE o.id=t.id RETURNING o.id
)
SELECT json_build_object('updated', (SELECT count(*) FROM updated));
""",
            org=canonical(state.organization_id),
            question=canonical(question_id),
            seconds=str(seconds),
        )
        if result != {"updated": 1}:
            raise FixtureError("exact owned delivery fault injection failed")


class FixtureController:
    def __init__(
        self,
        config: Config,
        state_path: Path,
        marker: str,
        *,
        api: Any = None,
        database: Any = None,
        runner_cleaner: Any = None,
        temporal: Any = None,
    ) -> None:
        if not isinstance(marker, str) or not MARKER.fullmatch(marker):
            raise FixtureError("invalid ownership marker")
        explicit_model = os.getenv("TEST_GATEWAY_MODEL_ID")
        if explicit_model and explicit_model != f"{marker}-model":
            raise FixtureError("configured model does not match marker")
        self.config = config
        self.state_path = state_path
        self.marker = marker
        self.model_id = f"{marker}-model"
        self.api = api or ApiClient(config)
        self.database = database or Database(config)
        self.runner_cleaner = runner_cleaner or DockerRunnerCleaner()
        self.temporal = temporal or TemporalTransport(config)

    def token(self, member: bool = False, plane: str = "admin") -> str:
        config = self.config
        return self.api.token(
            config.member_username if member else config.admin_username,
            config.member_password if member else config.admin_password,
            "admin-ui" if plane == "admin" else "business-ui",
        )

    def call(
        self,
        method: str,
        path: str,
        token: str,
        body: Any = None,
        *,
        plane: str = "admin",
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        response = self.api.request(
            plane, method, path, token, body=body, expected=expected
        )
        if method in {"GET", "POST", "PUT"} and isinstance(response, dict):
            return response.get("data", response)
        return response

    def _state(self) -> FixtureState:
        state = load_state(self.state_path)
        if state.marker != self.marker:
            raise FixtureError("journal marker mismatch")
        return state

    def _inventory(self, state: FixtureState) -> dict[str, Any]:
        return self.database.inventory(
            state.organization_id, self.model_id, self.marker
        )

    def prepare(self) -> None:
        if self.state_path.exists():
            raise FixtureError("recovery journal already exists")
        state = FixtureState(marker=self.marker)
        save_state(self.state_path, state)
        admin = self.call(
            "GET", "/v1/auth/me", self.token(plane="business"), plane="business"
        )
        member = self.call(
            "GET", "/v1/auth/me", self.token(True, "business"), plane="business"
        )
        state.organization_id = canonical(admin.get("org_id"))
        state.admin_id = canonical(admin.get("user_id"))
        state.member_id = canonical(member.get("user_id"))
        if member.get("org_id") != state.organization_id or state.admin_id == state.member_id:
            raise FixtureError("fixture identities do not share the expected organization")
        inventory = self._inventory(state)
        owned = self._discover(state, inventory)
        if any(owned.values()):
            raise FixtureError("ownership marker collision")
        state.original_model_grants = inventory["model_grants"]
        save_state(self.state_path, state)
        self.call(
            "PUT",
            "/v1/admin/authorization/grants/reconcile",
            self.token(),
            {
                "resourceType": "model",
                "resourceId": self.model_id,
                "grants": [
                    {
                        "principalType": "user",
                        "principalId": state.member_id,
                        "permission": "read",
                    }
                ],
            },
        )
        state.prepared = True
        save_state(self.state_path, state)

    def model_access(self, enabled: bool) -> None:
        state = self._state()
        if not state.prepared or state.original_model_grants is None:
            raise FixtureError("owned model fixture is not prepared")
        self.call("PUT", "/v1/admin/authorization/grants/reconcile", self.token(), {
            "resourceType": "model", "resourceId": self.model_id,
            "grants": [{"principalType": "user", "principalId": state.member_id,
                        "permission": "read"}] if enabled else [],
        })

    def revoke(self) -> None:
        self.model_access(False)

    def grant(self) -> None:
        self.model_access(True)

    def _discover(self, state: FixtureState, data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        owned = {
            key: []
            for key in ("conversation", "turn", "execution", "operation", "question", "outbox")
        }
        allowed_titles = {
            f"{self.marker} {suffix}"[:60]
            for suffix in ("ASK_CITY_THEN_FINAL", "ASK_TWICE", "ASK_LOOP")
        }
        for row in data["conversation"]:
            canonical(row.get("id"))
            if row["id"] not in state.conversation_ids and not str(
                row.get("title") or ""
            ).startswith(self.marker):
                continue
            if row.get("title") not in allowed_titles:
                raise FixtureError("unexpected marker collision")
            if (
                row.get("org_id") != state.organization_id
                or row.get("user_id") != state.member_id
                or row.get("has_attachments")
                or row.get("has_workflow_runs")
            ):
                raise FixtureError("conversation ownership mismatch")
            owned["conversation"].append(row)
        conversation_ids = {row["id"] for row in owned["conversation"]}
        for row in data["turn"]:
            canonical(row.get("id"))
            if (
                row["id"] not in state.turn_ids
                and row.get("conversation_id") not in conversation_ids
            ):
                continue
            if (
                row.get("org_id") != state.organization_id
                or row.get("owner_id") != state.member_id
                or row.get("conversation_id") not in conversation_ids
            ):
                raise FixtureError("turn ownership mismatch")
            owned["turn"].append(row)
        turn_ids = {row["id"] for row in owned["turn"]}
        for row in data["execution"]:
            canonical(row.get("id"))
            if (
                row["id"] not in state.execution_ids
                and row.get("turn_id") not in turn_ids
            ):
                continue
            turn_id = row.get("turn_id")
            turn = next((item for item in owned["turn"] if item["id"] == turn_id), None)
            if turn is None:
                raise FixtureError("execution turn mismatch")
            conversation_id = turn["conversation_id"]
            if (
                row.get("org_id") != state.organization_id
                or row.get("workflow_id") != f"conversation-{conversation_id}"
                or row.get("task_queue") != f"session-{conversation_id}"
                or row.get("phase") != "ordinary"
                or row.get("node_key") is not None
                or row.get("agent_definition_id") is not None
                or row.get("iteration") != 1
            ):
                raise FixtureError("execution ownership mismatch")
            run_id = canonical(row.get("run_id"))
            workflow_id = row["workflow_id"]
            previous = state.temporal_runs.get(workflow_id)
            if previous is not None and previous != run_id:
                raise FixtureError("Temporal root run changed")
            state.temporal_runs[workflow_id] = run_id
            owned["execution"].append(row)
        execution_ids = {row["id"] for row in owned["execution"]}
        for row in data["operation"]:
            canonical(row.get("id"))
            if (
                row["id"] not in state.operation_ids
                and row.get("execution_id") not in execution_ids
            ):
                continue
            if (
                row.get("org_id") != state.organization_id
                or row.get("turn_id") not in turn_ids
                or row.get("execution_id") not in execution_ids
                or row.get("kind") != "ask_user"
            ):
                raise FixtureError("operation ownership mismatch")
            owned["operation"].append(row)
        operation_ids = {row["id"] for row in owned["operation"]}
        for row in data["question"]:
            canonical(row.get("id"))
            if (
                row["id"] not in state.question_ids
                and row.get("operation_id") not in operation_ids
            ):
                continue
            if (
                row.get("org_id") != state.organization_id
                or row.get("turn_id") not in turn_ids
                or row.get("execution_id") not in execution_ids
                or row.get("operation_id") not in operation_ids
                or row.get("text") not in QUESTION_TEXTS
            ):
                raise FixtureError("question ownership mismatch")
            owned["question"].append(row)
        question_ids = {row["id"] for row in owned["question"]}
        for row in data["outbox"]:
            canonical(row.get("id"))
            if (
                row["id"] not in state.outbox_ids
                and row.get("question_id") not in question_ids
            ):
                continue
            if (
                row.get("org_id") != state.organization_id
                or row.get("turn_id") not in turn_ids
                or row.get("execution_id") not in execution_ids
                or row.get("question_id") not in question_ids
            ):
                raise FixtureError("outbox ownership mismatch")
            owned["outbox"].append(row)

        saved = {
            "conversation": state.conversation_ids,
            "turn": state.turn_ids,
            "execution": state.execution_ids,
            "operation": state.operation_ids,
            "question": state.question_ids,
            "outbox": state.outbox_ids,
        }
        for kind, identifiers in saved.items():
            current = {row["id"] for row in data[kind]}
            proven = {row["id"] for row in owned[kind]}
            if set(identifiers) & current - proven:
                raise FixtureError("recorded resource ownership mismatch")
        state.conversation_ids = sorted(set(state.conversation_ids) | conversation_ids)
        state.turn_ids = sorted(set(state.turn_ids) | turn_ids)
        state.execution_ids = sorted(set(state.execution_ids) | execution_ids)
        state.operation_ids = sorted(set(state.operation_ids) | operation_ids)
        state.question_ids = sorted(set(state.question_ids) | question_ids)
        state.outbox_ids = sorted(
            set(state.outbox_ids) | {row["id"] for row in owned["outbox"]}
        )
        return owned

    def record(self, kind: str, resource_id: str) -> None:
        canonical(resource_id)
        if kind not in {"conversation", "turn", "execution", "operation", "question"}:
            raise FixtureError("invalid record kind")
        state = self._state()
        owned = self._discover(state, self._inventory(state))
        if resource_id not in {row["id"] for row in owned[kind]}:
            raise FixtureError("record resource ownership mismatch")
        save_state(self.state_path, state)

    def _validate_temporal_roots(
        self, state: FixtureState
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for workflow_id, run_id in state.temporal_runs.items():
            root = self.temporal.read(workflow_id, run_id)
            conversation_id = workflow_id.removeprefix("conversation-")
            if (
                not isinstance(root, dict)
                or root.get("workflow_id") != workflow_id
                or root.get("run_id") != run_id
                or root.get("workflow_type") != "ConversationWorkflow"
                or root.get("task_queue") != f"session-{conversation_id}"
                or root.get("input", {}).get("conversation_id") != conversation_id
                or root.get("parent") is not None
                or root.get("continued")
                or root.get("pending_children")
                or root.get("status")
                not in {
                    "RUNNING",
                    "COMPLETED",
                    "FAILED",
                    "CANCELED",
                    "TERMINATED",
                    "TIMED_OUT",
                }
            ):
                raise FixtureError("Temporal execution identity mismatch")
            children = root.get("children")
            if not isinstance(children, list):
                raise FixtureError("Temporal execution identity mismatch")
            records.append(root)
            seen: set[tuple[str, str]] = set()
            for link in children:
                if (
                    not isinstance(link, dict)
                    or not isinstance(link.get("workflow_id"), str)
                    or not isinstance(link.get("run_id"), str)
                    or type(link.get("initiated_event_id")) is not int
                    or link["initiated_event_id"] <= 0
                ):
                    raise FixtureError("Temporal execution is not journal-owned")
                child_id = link["workflow_id"]
                child_run = canonical(link["run_id"])
                if (child_id, child_run) in seen:
                    raise FixtureError("Temporal execution is not journal-owned")
                seen.add((child_id, child_run))
                child = self.temporal.read(child_id, child_run)
                child_input = child.get("input", {}) if isinstance(child, dict) else {}
                turn_id = child_input.get("turn_id")
                expected_parent = {
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                    "initiated_event_id": link["initiated_event_id"],
                }
                if (
                    not isinstance(child, dict)
                    or turn_id not in state.turn_ids
                    or child_id != f"{conversation_id}-eval-{turn_id}"
                    or child.get("workflow_id") != child_id
                    or child.get("run_id") != child_run
                    or child.get("workflow_type") != "EvaluationWorkflow"
                    or child.get("task_queue") != f"session-{conversation_id}"
                    or child_input.get("conversation_id") != conversation_id
                    or child.get("parent") != expected_parent
                    or child.get("children") != []
                    or child.get("continued")
                    or child.get("pending_children")
                    or child.get("status")
                    not in {
                        "RUNNING",
                        "COMPLETED",
                        "FAILED",
                        "CANCELED",
                        "TERMINATED",
                        "TIMED_OUT",
                    }
                ):
                    raise FixtureError("Temporal execution is not journal-owned")
                records.append(child)
        return records

    def _discover_temporal_roots(self, state: FixtureState) -> None:
        """Pin exact conversation runs even if interaction registration crashed."""
        for conversation_id in state.conversation_ids:
            workflow_id = f"conversation-{conversation_id}"
            if workflow_id in state.temporal_runs:
                continue
            root = self.temporal.read(workflow_id)
            if root is None:
                continue
            if not isinstance(root, dict):
                raise FixtureError("Temporal execution identity mismatch")
            state.temporal_runs[workflow_id] = canonical(root.get("run_id"))
        # A discovered run is journal-owned only after the complete root and
        # reciprocal child proof succeeds.
        self._validate_temporal_roots(state)

    def _owned_chain(
        self, kind: str, resource_id: str
    ) -> tuple[FixtureState, dict[str, list[dict[str, Any]]]]:
        canonical(resource_id)
        state = self._state()
        owned = self._discover(state, self._inventory(state))
        self._discover_temporal_roots(state)
        if resource_id not in {row["id"] for row in owned[kind]}:
            raise FixtureError(f"resource is not a recorded owned {kind}")
        save_state(self.state_path, state)
        return state, owned

    def stop_worker(self, conversation_id: str) -> None:
        self._owned_chain("conversation", conversation_id)
        self.runner_cleaner.stop_owned(conversation_id)

    def start_worker(self, conversation_id: str) -> None:
        self._owned_chain("conversation", conversation_id)
        self.runner_cleaner.start_owned(conversation_id)

    def defer_delivery(self, question_id: str, seconds: int) -> None:
        if not 1 <= seconds <= 300:
            raise FixtureError("delivery fault delay must be between 1 and 300 seconds")
        state, _ = self._owned_chain("question", question_id)
        self.database.defer_delivery(state, question_id, seconds)
        # Capture the exact version-two outbox row created by the answer.
        self._discover(state, self._inventory(state))
        save_state(self.state_path, state)

    def _recover_temporal(self, state: FixtureState, *, close: bool) -> None:
        roots = self._validate_temporal_roots(state)
        if close:
            for root in roots:
                if root["status"] == "RUNNING":
                    self.temporal.terminate(root["workflow_id"], root["run_id"])
            roots = self._validate_temporal_roots(state)
        if any(root["status"] == "RUNNING" for root in roots):
            raise FixtureError("Temporal owned execution remains open")

    def _restore(self, state: FixtureState) -> list[str]:
        if state.original_model_grants is None:
            return []
        try:
            response = self.call(
                "PUT",
                "/v1/admin/authorization/grants/reconcile",
                self.token(),
                {
                    "resourceType": "model",
                    "resourceId": self.model_id,
                    "grants": state.original_model_grants,
                },
                expected=(200, 404),
            )
            if response == {"_httpStatus": 404}:
                self.database.restore_model_grants(state, self.model_id)
            return []
        except Exception:  # noqa: BLE001 - aggregate cleanup failures
            return ["model grant restoration failed"]

    def cleanup(self) -> None:
        if not self.state_path.exists():
            return
        state = self._state()
        if not state.organization_id:
            return
        errors: list[str] = []
        try:
            owned = self._discover(state, self._inventory(state))
            self._discover_temporal_roots(state)
            save_state(self.state_path, state)
            self._recover_temporal(state, close=True)
            for conversation in owned["conversation"]:
                self.call(
                    "DELETE",
                    f"/v1/conversations/{conversation['id']}",
                    self.token(True, "business"),
                    plane="business",
                    expected=(204, 404),
                )
            for conversation in owned["conversation"]:
                self.runner_cleaner.remove_owned(conversation["id"])
            # The chat request can create its root between the first exact
            # lookup and API cancellation. Re-check after the owned runner is
            # stopped, pin and close that exact run before any hard delete.
            self._discover_temporal_roots(state)
            save_state(self.state_path, state)
            self._recover_temporal(state, close=True)
            for conversation in owned["conversation"]:
                self.database.delete_conversation(state, conversation)
            for conversation_id in set(state.conversation_ids) - {
                row["id"] for row in owned["conversation"]
            }:
                self.runner_cleaner.remove_owned(conversation_id)
        except Exception as exc:  # noqa: BLE001 - aggregate restoration below
            errors.append(
                str(exc)
                if isinstance(exc, FixtureError)
                else "owned cleanup failed; journal retained"
            )
        errors.extend(self._restore(state))
        if errors:
            raise FixtureError("; ".join(errors))

    def verify_clean(self) -> None:
        if not self.state_path.exists():
            return
        state = self._state()
        if not state.organization_id:
            return
        self._discover_temporal_roots(state)
        save_state(self.state_path, state)
        self._recover_temporal(state, close=False)
        data = self._inventory(state)
        owned = self._discover(state, data)
        if any(owned.values()):
            raise FixtureError("owned resources remain")
        if state.original_model_grants is not None:
            actual = sorted(json.dumps(row, sort_keys=True) for row in data["model_grants"])
            expected = sorted(
                json.dumps(row, sort_keys=True) for row in state.original_model_grants
            )
            if actual != expected:
                raise FixtureError("grant restoration mismatch")
        for conversation_id in state.conversation_ids:
            self.runner_cleaner.assert_absent(conversation_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "prepare",
        "record",
        "stop-worker",
        "start-worker",
        "defer-delivery",
        "cleanup",
        "verify-clean",
        "revoke",
        "grant",
    ):
        command = commands.add_parser(name)
        command.add_argument("--state", required=True, type=Path)
        command.add_argument("--marker", required=True)
        if name == "record":
            command.add_argument(
                "--kind",
                choices=("conversation", "turn", "execution", "operation", "question"),
                required=True,
            )
            command.add_argument("--id", required=True)
        elif name in {"stop-worker", "start-worker"}:
            command.add_argument("--conversation", required=True)
        elif name == "defer-delivery":
            command.add_argument("--question", required=True)
            command.add_argument("--seconds", required=True, type=int)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        controller = FixtureController(Config(), args.state, args.marker)
        if args.command == "record":
            controller.record(args.kind, args.id)
        elif args.command in {"stop-worker", "start-worker"}:
            getattr(controller, args.command.replace("-", "_"))(args.conversation)
        elif args.command == "defer-delivery":
            controller.defer_delivery(args.question, args.seconds)
        else:
            getattr(controller, args.command.replace("-", "_"))()
    except Exception as exc:  # noqa: BLE001 - operator-safe boundary
        detail = str(exc) if isinstance(exc, FixtureError) else "operation failed"
        print(
            f"[user-questions-e2e] {detail}; preserve recovery journal",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
