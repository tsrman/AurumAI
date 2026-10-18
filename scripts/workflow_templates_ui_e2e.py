#!/usr/bin/env python3
"""Private, exact-owned lifecycle for the manual workflow-template acceptance.

No template/instantiation mutations: the browser owns those operations. Run
exclusively against the built-in organization; this is not a cross-smoke lock.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field, fields
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import uuid

MARKER = re.compile(r"workflow-templates-ui-e2e-[a-f0-9]{16}")


class FixtureError(RuntimeError):
    """Operational error without credentials, response bodies or internal errors."""


def env(name: str, default: str) -> str:
    return os.getenv(name, default).rstrip("/")


@dataclass(frozen=True)
class Config:
    business_url: str = field(
        default_factory=lambda: env("E2E_BUSINESS_BACKEND_URL", "http://localhost:8000")
    )
    admin_url: str = field(
        default_factory=lambda: env("E2E_ADMIN_BACKEND_URL", "http://localhost:8001")
    )
    keycloak_url: str = field(
        default_factory=lambda: env("E2E_KEYCLOAK_URL", "http://localhost:8080")
    )
    postgres_container: str = field(
        default_factory=lambda: env("E2E_POSTGRES_CONTAINER", "aurum-ai-postgres")
    )
    backend_container: str = field(
        default_factory=lambda: env("E2E_BACKEND_CONTAINER", "aurum-ai-backend")
    )
    realm: str = field(default_factory=lambda: env("E2E_KEYCLOAK_REALM", "aurum-ai"))
    admin_username: str = field(default_factory=lambda: env("E2E_ADMIN_USERNAME", "admin"))
    admin_password: str = field(
        default_factory=lambda: env("E2E_ADMIN_PASSWORD", "admin"), repr=False
    )
    member_username: str = field(default_factory=lambda: env("E2E_MEMBER_USERNAME", "user"))
    member_password: str = field(
        default_factory=lambda: env("E2E_MEMBER_PASSWORD", "user"), repr=False
    )


@dataclass
class FixtureState:
    marker: str
    organization_id: str = ""
    admin_id: str = ""
    member_id: str = ""
    original_capabilities: list[str] | None = None
    original_model_grants: list[dict[str, Any]] | None = None
    source_agent_ids: dict[str, str] = field(default_factory=dict)
    source_workflow_id: str = ""
    template_ids: list[str] = field(default_factory=list)
    workflow_ids: list[str] = field(default_factory=list)
    agent_ids: list[str] = field(default_factory=list)
    conversation_ids: list[str] = field(default_factory=list)
    workflow_run_ids: list[str] = field(default_factory=list)
    # Minimal graph proof survives a workflow deletion followed by agent timeout.
    agent_proofs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Pin the execution, not only a reusable workflow ID. None proves NOT_FOUND
    # before any turn; established history must never silently become absent.
    temporal_root_run_ids: dict[str, str | None] = field(default_factory=dict)
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
    for key in ("organization_id", "admin_id", "member_id", "source_workflow_id"):
        value = getattr(state, key)
        if not isinstance(value, str):
            raise FixtureError("invalid journal schema")
        if value:
            canonical(value)
    if not isinstance(state.prepared, bool):
        raise FixtureError("invalid journal schema")
    if not isinstance(state.source_agent_ids, dict) or set(state.source_agent_ids) - {"v1", "v2"}:
        raise FixtureError("invalid source agent schema")
    for value in state.source_agent_ids.values():
        canonical(value)
    for key in (
        "template_ids",
        "workflow_ids",
        "agent_ids",
        "conversation_ids",
        "workflow_run_ids",
    ):
        values = getattr(state, key)
        if (
            not isinstance(values, list)
            or not all(isinstance(v, str) for v in values)
            or len(values) != len(set(values))
        ):
            raise FixtureError("invalid resource journal schema")
        for value in values:
            canonical(value)
    if state.original_capabilities is not None:
        caps = state.original_capabilities
        if not isinstance(caps, list) or not all(
            isinstance(v, str) and re.fullmatch(r"[a-z][a-z0-9_.-]{1,99}", v) for v in caps
        ):
            raise FixtureError("invalid capabilities schema")
    if state.original_model_grants is not None:
        if not isinstance(state.original_model_grants, list):
            raise FixtureError("invalid grants schema")
        for grant in state.original_model_grants:
            if not isinstance(grant, dict) or set(grant) != {
                "principalType",
                "principalId",
                "permission",
            }:
                raise FixtureError("invalid grants schema")
            canonical(grant["principalId"])
            if grant["principalType"] not in {"organization", "user", "group"} or grant[
                "permission"
            ] not in {"read", "write"}:
                raise FixtureError("invalid grants schema")
    if not isinstance(state.agent_proofs, dict):
        raise FixtureError("invalid agent proof schema")
    for id, proof in state.agent_proofs.items():
        canonical(id)
        if not isinstance(proof, dict) or set(proof) != {
            "name",
            "workflow_id",
            "template_id",
            "template_version",
        }:
            raise FixtureError("invalid agent proof schema")
        if (
            id not in state.agent_ids
            or proof["workflow_id"] not in state.workflow_ids
            or not isinstance(proof["name"], str)
        ):
            raise FixtureError("invalid agent proof ownership")
        if (
            proof["template_id"] not in state.template_ids
            or type(proof["template_version"]) is not int
            or proof["template_version"] not in {1, 2}
        ):
            raise FixtureError("invalid agent proof provenance")
    if not isinstance(state.temporal_root_run_ids, dict):
        raise FixtureError("invalid Temporal journal schema")
    for conversation, run in state.temporal_root_run_ids.items():
        if conversation not in state.conversation_ids:
            raise FixtureError("invalid Temporal journal ownership")
        if run is not None:
            canonical(run)
    has_resources = (
        state.source_agent_ids
        or state.source_workflow_id
        or any(
            getattr(state, k)
            for k in (
                "template_ids",
                "workflow_ids",
                "agent_ids",
                "conversation_ids",
                "workflow_run_ids",
            )
        )
    )
    if (
        has_resources
        or state.original_capabilities is not None
        or state.original_model_grants is not None
    ) and not all((state.organization_id, state.admin_id, state.member_id)):
        raise FixtureError("missing ownership identity")


def private_parent(path: Path) -> None:
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700, parents=False)
    if (
        path.parent.is_symlink()
        or stat.S_IMODE(path.parent.stat().st_mode) != 0o700
        or path.parent.stat().st_uid != os.getuid()
    ):
        raise FixtureError(
            "journal requires a private mode700 parent; shared parents are not chmodded"
        )
    if path.is_symlink():
        raise FixtureError("journal cannot be a symlink")


def save_state(path: Path, state: FixtureState) -> None:
    validate_state(state)
    private_parent(path)
    fd, name = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(asdict(state), stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load_state(path: Path) -> FixtureState:
    private_parent(path)
    if stat.S_IMODE(path.stat().st_mode) != 0o600 or path.stat().st_uid != os.getuid():
        raise FixtureError("journal requires private mode600")
    try:
        raw = json.loads(path.read_text())
        expected = {f.name for f in fields(FixtureState)}
        # Only this explicitly added field may be absent in Task10/11 journals.
        if not isinstance(raw, dict) or set(raw) not in (
            expected,
            expected - {"temporal_root_run_ids"},
        ):
            raise ValueError
        state = FixtureState(**raw)
    except (OSError, ValueError, TypeError) as exc:
        raise FixtureError("invalid journal schema") from exc
    validate_state(state)
    return state


class ApiClient:
    def __init__(self, config: Config):
        self.config = config

    def token(self, username: str, password: str, audience: str) -> str:
        request = urllib.request.Request(
            f"{self.config.keycloak_url}/realms/{self.config.realm}/protocol/openid-connect/token",
            data=urllib.parse.urlencode(
                dict(
                    grant_type="password", client_id=audience, username=username, password=password
                )
            ).encode(),
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                value = json.load(response)["access_token"]
            if not isinstance(value, str) or not value:
                raise ValueError
            return value
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise FixtureError("authentication failed") from exc

    def request(self, plane, method, path, token, *, body=None, expected=(200,)):
        base = self.config.admin_url if plane == "admin" else self.config.business_url
        request = urllib.request.Request(
            base + path,
            method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            data=None if body is None else json.dumps(body).encode(),
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, b""
        except OSError as exc:
            raise FixtureError("HTTP transport failed; outcome unknown") from exc
        if status not in expected:
            raise FixtureError(f"HTTP {method} failed ({status})")
        if status == 404:
            return {"_httpStatus": 404}
        try:
            return json.loads(raw) if raw else None
        except ValueError as exc:
            raise FixtureError("invalid API response") from exc


class Database:
    def __init__(self, config: Config):
        self.config = config

    def query(self, sql: str, **parameters: str):
        # psql :'name' expands a quoted SQL literal, not SQL source interpolation.
        command = [
            "docker",
            "exec",
            "-i",
            self.config.postgres_container,
            "psql",
            "-X",
            "-q",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            "aurum_ai",
        ]
        for key, value in parameters.items():
            command.extend(["-v", f"{key}={value}"])
        result = subprocess.run(command, input=sql, capture_output=True, text=True, check=False)
        if result.returncode:
            raise FixtureError("owned database operation failed")
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except ValueError as exc:
            raise FixtureError("invalid database result") from exc

    def inventory(self, org: str, model: str):
        return self.query(
            """
SELECT json_build_object(
'agent', (SELECT coalesce(json_agg(t),'[]') FROM (SELECT id, org_id, created_by, name FROM agent_definitions WHERE org_id=:'org'::uuid) t),
'template', (SELECT coalesce(json_agg(t),'[]') FROM (SELECT id, org_id, created_by, name FROM workflow_templates WHERE org_id=:'org'::uuid) t),
'workflow', (SELECT coalesce(json_agg(t),'[]') FROM (SELECT w.id, w.org_id, w.created_by, w.name, w.source_template_id, w.source_template_version,
 ARRAY(SELECT w.supervisor_agent_definition_id UNION SELECT n.agent_definition_id FROM workflow_nodes n WHERE n.org_id=w.org_id AND n.workflow_definition_id=w.id) AS agent_ids
 FROM workflow_definitions w WHERE org_id=:'org'::uuid) t),
'conversation', (SELECT coalesce(json_agg(t),'[]') FROM (SELECT c.id, c.org_id, c.user_id, c.title,
 EXISTS(SELECT 1 FROM knowledge_bases k WHERE k.owning_conversation_id=c.id) AS has_attachments
 FROM conversations c WHERE org_id=:'org'::uuid) t),
'run', (SELECT coalesce(json_agg(t),'[]') FROM (SELECT r.id, r.org_id, r.initiating_user_id, r.conversation_id, r.workflow_definition_id,
 ARRAY(SELECT n.agent_definition_id FROM workflow_node_runs n WHERE n.org_id=r.org_id AND n.workflow_run_id=r.id) AS agent_ids
 FROM workflow_runs r WHERE org_id=:'org'::uuid) t),
'capabilities', (SELECT coalesce(json_agg(capability ORDER BY capability),'[]') FROM organization_capabilities WHERE org_id=:'org'::uuid),
'model_grants', (SELECT coalesce(json_agg(t),'[]') FROM (SELECT principal_type AS "principalType", principal_id AS "principalId", permission FROM access_grants WHERE org_id=:'org'::uuid AND resource_type='model' AND resource_id=:'model') t)
);
""",
            org=canonical(org),
            model=model,
        )

    def restore_model_grants(self, state: FixtureState, model: str):
        validate_state(state)
        if model != f"{state.marker}-model" or state.original_model_grants != []:
            raise FixtureError("model grant fallback requires exact owned empty baseline")
        self.query(
            "DELETE FROM access_grants WHERE org_id=:'org'::uuid AND resource_type='model' AND resource_id=:'model';",
            org=canonical(state.organization_id),
            model=model,
        )

    def delete_owned(self, state: FixtureState, owned: dict[str, list[dict[str, Any]]]):
        # Definitions are archive-only, conversations are soft-delete. Hard-delete
        # those exact rows here after stop/ownership checks; agents use their API.
        payload = json.dumps({"run_definition": [], **owned})
        statements = [
            "BEGIN",
            "CREATE TEMP TABLE fixture_owned (kind text, id uuid, name text, owner_id uuid, provenance uuid, provenance_version integer) ON COMMIT DROP",
        ]
        for kind in ("workflow", "template", "conversation", "run", "run_definition"):
            statements.append(
                f"""INSERT INTO fixture_owned SELECT '{kind}', (r->>'id')::uuid, coalesce(r->>'name',r->>'title'), coalesce(r->>'created_by',r->>'user_id',r->>'initiating_user_id')::uuid, (r->>'source_template_id')::uuid, (r->>'source_template_version')::integer FROM jsonb_array_elements(:'owned'::jsonb->'{kind}') r"""
            )
        statements.extend(
            [
                "DELETE FROM access_grants g USING fixture_owned f WHERE g.org_id=:'org'::uuid AND g.resource_type=f.kind AND g.resource_id=f.id::text",
                "UPDATE llm_usage_events SET workflow_run_id=NULL, workflow_phase=NULL, workflow_node_key=NULL WHERE org_id=:'org'::uuid AND workflow_run_id IN (SELECT id FROM fixture_owned WHERE kind='run')",
                """DELETE FROM interaction_turns t
USING fixture_owned c, conversations owned_c
WHERE c.kind='conversation'
AND owned_c.id=c.id AND owned_c.org_id=:'org'::uuid
AND owned_c.user_id=c.owner_id AND owned_c.title=c.name
AND NOT EXISTS (SELECT 1 FROM knowledge_bases k WHERE k.owning_conversation_id=owned_c.id)
AND t.org_id=:'org'::uuid AND t.conversation_id=c.id AND t.owner_id=c.owner_id
AND (t.workflow_run_id IS NULL OR EXISTS (
 SELECT 1 FROM workflow_runs r JOIN fixture_owned f ON f.kind='run' AND f.id=r.id
 WHERE r.id=t.workflow_run_id AND r.org_id=:'org'::uuid
 AND r.initiating_user_id=f.owner_id AND f.owner_id=c.owner_id
 AND r.conversation_id=c.id
 AND r.workflow_definition_id IN (SELECT id FROM fixture_owned WHERE kind IN ('workflow', 'run_definition'))
))""",
                """DELETE FROM workflow_runs r
USING fixture_owned f, fixture_owned c, conversations owned_c
WHERE f.kind='run' AND c.kind='conversation'
AND r.id=f.id AND r.org_id=:'org'::uuid
AND r.initiating_user_id=f.owner_id AND f.owner_id=c.owner_id
AND r.conversation_id=c.id
AND owned_c.id=c.id AND owned_c.org_id=:'org'::uuid
AND owned_c.user_id=c.owner_id AND owned_c.title=c.name
AND NOT EXISTS (SELECT 1 FROM knowledge_bases k WHERE k.owning_conversation_id=owned_c.id)
AND r.workflow_definition_id IN (SELECT id FROM fixture_owned WHERE kind IN ('workflow', 'run_definition'))""",
                "DELETE FROM conversations c USING fixture_owned f WHERE f.kind='conversation' AND c.id=f.id AND c.org_id=:'org'::uuid AND c.user_id=f.owner_id AND c.title=f.name AND NOT EXISTS (SELECT 1 FROM knowledge_bases k WHERE k.owning_conversation_id=c.id) AND NOT EXISTS (SELECT 1 FROM workflow_runs r WHERE r.conversation_id=c.id)",
                "DELETE FROM workflow_definitions w USING fixture_owned f WHERE f.kind='workflow' AND w.id=f.id AND w.org_id=:'org'::uuid AND w.created_by=f.owner_id AND w.name=f.name AND w.source_template_id IS NOT DISTINCT FROM f.provenance AND w.source_template_version IS NOT DISTINCT FROM f.provenance_version",
                "DELETE FROM workflow_templates t USING fixture_owned f WHERE f.kind='template' AND t.id=f.id AND t.org_id=:'org'::uuid AND t.created_by=f.owner_id AND t.name=f.name",
                """DO $cleanup$
BEGIN
 IF EXISTS (
  SELECT 1 FROM fixture_owned f WHERE
  (f.kind='conversation' AND EXISTS (SELECT 1 FROM conversations c WHERE c.id=f.id))
  OR (f.kind='run' AND EXISTS (SELECT 1 FROM workflow_runs r WHERE r.id=f.id))
  OR (f.kind='workflow' AND EXISTS (SELECT 1 FROM workflow_definitions w WHERE w.id=f.id))
  OR (f.kind='template' AND EXISTS (SELECT 1 FROM workflow_templates t WHERE t.id=f.id))
 ) THEN
  RAISE EXCEPTION 'owned cleanup target remains';
 END IF;
END
$cleanup$""",
                "COMMIT",
            ]
        )
        self.query(
            ";\n".join(statements) + ";", org=canonical(state.organization_id), owned=payload
        )


class DockerRunnerCleaner:
    def inspect(self, conversation_id):
        canonical(conversation_id)
        result = subprocess.run(
            ["docker", "inspect", f"runner-{conversation_id}"], capture_output=True, text=True
        )
        if result.returncode:
            if (
                "no such object" in result.stderr.lower()
                or "no such container" in result.stderr.lower()
            ):
                return None
            raise FixtureError("runner inspection failed")
        try:
            (doc,) = json.loads(result.stdout)
            if (
                doc["Config"]["Labels"].get("aurum-ai.component") != "conversation-runner"
                or f"CONVERSATION_ID={conversation_id}" not in doc["Config"]["Env"]
                or not doc["Id"]
            ):
                raise ValueError
            return doc["Id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise FixtureError("runner ownership mismatch") from exc

    def remove_owned(self, conversation_id):
        container = self.inspect(conversation_id)
        if (
            container
            and subprocess.run(["docker", "rm", "-f", container], capture_output=True).returncode
        ):
            raise FixtureError("owned runner removal failed")

    def assert_absent(self, conversation_id):
        if self.inspect(conversation_id):
            raise FixtureError("owned runner remains")

    def stop_owned(self, conversation_id: str) -> None:
        container = self.inspect(conversation_id)
        if container is None:
            raise FixtureError("owned runner is absent")
        result = subprocess.run(
            ["docker", "stop", container], capture_output=True, text=True
        )
        if result.returncode:
            raise FixtureError("owned runner stop failed")

    def start_owned(self, conversation_id: str) -> None:
        container = self.inspect(conversation_id)
        if container is None:
            raise FixtureError("owned runner is absent")
        result = subprocess.run(
            ["docker", "start", container], capture_output=True, text=True
        )
        if result.returncode:
            raise FixtureError("owned runner start failed")
        for _ in range(120):
            state = subprocess.run(
                ["docker", "inspect", container, "--format", "{{json .State}}"],
                capture_output=True,
                text=True,
            )
            try:
                data = json.loads(state.stdout)
                health = data.get("Health", {}).get("Status")
                if state.returncode == 0 and data.get("Running") and health in {
                    None,
                    "healthy",
                }:
                    return
            except (AttributeError, TypeError, ValueError):
                pass
            time.sleep(0.5)
        raise FixtureError("owned runner readiness failed")


# Run inside the already-required Compose backend, whose installed SDK and
# namespace configuration match the stack. Only allowlisted identity metadata
# crosses stdout; payloads, signals, results and exception text never do.
TEMPORAL_RPC = r"""
import asyncio, json, os, sys
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

async def main():
    request = json.load(sys.stdin)
    client = await Client.connect(
        os.environ["APP_TEMPORAL_HOST"],
        namespace=os.environ["APP_TEMPORAL_NAMESPACE"],
    )
    handle = client.get_workflow_handle(request["workflow_id"], run_id=request["run_id"])
    try:
        description = await handle.describe()
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND and request["action"] == "read":
            return None
        raise
    handle = client.get_workflow_handle(description.id, run_id=description.run_id)
    if request["action"] == "terminate":
        if description.status.name == "RUNNING":
            await handle.terminate(reason="Exact-owned C5 browser fixture recovery")
        return None
    history = await handle.fetch_history()
    start = history.events[0].workflow_execution_started_event_attributes
    decoded = await client.data_converter.decode(start.input.payloads)
    if len(decoded) != 1 or not isinstance(decoded[0], dict):
        raise ValueError()
    inputs = {key: decoded[0][key] for key in (
        "conversation_id", "turn_id", "run_id", "workflow_run_id", "workflow_phase", "workflow_node_key"
    ) if key in decoded[0]}
    if any(value is not None and not isinstance(value, str) for value in inputs.values()):
        raise ValueError()
    parent = None
    if start.parent_workflow_execution.workflow_id:
        parent = dict(workflow_id=start.parent_workflow_execution.workflow_id,
                      run_id=start.parent_workflow_execution.run_id,
                      initiated_event_id=start.parent_initiated_event_id)
    children = []
    pending = set()
    continued = bool(start.continued_execution_run_id)
    for event in history.events:
        if event.HasField("start_child_workflow_execution_initiated_event_attributes"):
            pending.add(event.event_id)
        if event.HasField("child_workflow_execution_started_event_attributes"):
            child = event.child_workflow_execution_started_event_attributes
            pending.discard(child.initiated_event_id)
            children.append(dict(workflow_id=child.workflow_execution.workflow_id,
                                 run_id=child.workflow_execution.run_id,
                                 initiated_event_id=child.initiated_event_id))
        if event.HasField("workflow_execution_continued_as_new_event_attributes"):
            continued = True
        if event.HasField("start_child_workflow_execution_failed_event_attributes"):
            pending.discard(event.start_child_workflow_execution_failed_event_attributes.initiated_event_id)
        for kind in ("failed", "timed_out"):
            field = "workflow_execution_" + kind + "_event_attributes"
            if event.HasField(field) and getattr(event, field).new_execution_run_id:
                continued = True
    return dict(workflow_id=description.id, run_id=description.run_id,
                workflow_type=description.workflow_type, task_queue=description.task_queue,
                status=description.status.name, input=inputs, parent=parent,
                children=children, continued=continued, pending_children=bool(pending))

try:
    print(json.dumps(asyncio.run(asyncio.wait_for(main(), 45))))
except Exception as error:
    # Class and RPC status only: messages may echo workflow payloads.
    kind = type(error).__name__
    if isinstance(error, RPCError):
        kind += "/" + error.status.name
    print(f"Temporal RPC unavailable ({kind})", file=sys.stderr)
    sys.exit(1)
"""

RPC_FAILURE_KIND = re.compile(r"^Temporal RPC unavailable \(([A-Za-z_]{1,64}(?:/[A-Z_]{1,32})?)\)$", re.M)


class TemporalTransport:
    # read/terminate are idempotent (terminate re-describes and only stops a
    # RUNNING exact run), so transient exec/RPC failures are retried.
    attempts = 4
    retry_delay_seconds = 3

    def __init__(self, config):
        self.config = config
        self.checked = False

    def request(self, action, workflow_id, run_id):
        if not self.checked:
            labels = []
            for container in (self.config.postgres_container, self.config.backend_container):
                result = subprocess.run(
                    [
                        "docker",
                        "inspect",
                        container,
                        "--format",
                        '{{index .Config.Labels "com.docker.compose.project"}} '
                        '{{index .Config.Labels "com.docker.compose.service"}}',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if result.returncode:
                    raise FixtureError("Temporal Compose ownership unavailable")
                labels.append(result.stdout.strip().split())
            if (
                len(labels[0]) != 2
                or len(labels[1]) != 2
                or labels[0][0] != labels[1][0]
                or labels[0][1] != "postgres"
                or labels[1][1] != "backend"
            ):
                raise FixtureError("Temporal Compose ownership mismatch")
            self.checked = True
        kind = "exec"
        for attempt in range(self.attempts):
            if attempt:
                time.sleep(self.retry_delay_seconds * attempt)
            try:
                result = subprocess.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        self.config.backend_container,
                        "/app/.venv/bin/python",
                        "-c",
                        TEMPORAL_RPC,
                    ],
                    input=json.dumps(dict(action=action, workflow_id=workflow_id, run_id=run_id)),
                    capture_output=True,
                    text=True,
                    timeout=55,
                )
            except subprocess.TimeoutExpired:
                kind = "timeout"
                continue
            if not result.returncode:
                return json.loads(result.stdout)
            match = RPC_FAILURE_KIND.search(result.stderr or "")
            kind = match.group(1) if match else "exec"
        raise FixtureError(f"Temporal RPC unavailable ({kind}); journal retained")

    def read(self, workflow_id, run_id=None):
        return self.request("read", workflow_id, run_id)

    def terminate(self, workflow_id, run_id):
        return self.request("terminate", workflow_id, canonical(run_id))


class FixtureController:
    max_temporal_executions = 32

    def __init__(
        self,
        config: Config,
        state_path: Path,
        marker: str,
        *,
        api=None,
        database=None,
        runner_cleaner=None,
    ):
        if not isinstance(marker, str) or not MARKER.fullmatch(marker):
            raise FixtureError("invalid ownership marker")
        explicit_model = os.getenv("TEST_GATEWAY_MODEL_ID")
        if explicit_model and explicit_model != f"{marker}-model":
            raise FixtureError("configured model does not match marker")
        self.config, self.state_path, self.marker = config, state_path, marker
        self.model_id = f"{marker}-model"
        self.api = api or ApiClient(config)
        self.database = database or Database(config)
        self.runner_cleaner = runner_cleaner or DockerRunnerCleaner()
        self.temporal_transport = TemporalTransport(config)

    def _state(self):
        state = load_state(self.state_path)
        if state.marker != self.marker:
            raise FixtureError("journal marker mismatch")
        return state

    def token(self, member=False, plane="admin"):
        c = self.config
        return self.api.token(
            c.member_username if member else c.admin_username,
            c.member_password if member else c.admin_password,
            "admin-ui" if plane == "admin" else "business-ui",
        )

    def call(self, method, path, token, body=None, plane="admin", expected=(200,)):
        response = self.api.request(plane, method, path, token, body=body, expected=expected)
        if method in {"GET", "POST"} and isinstance(response, dict) and "data" in response:
            return response["data"]
        return response

    def prepare(self):
        if self.state_path.exists():
            raise FixtureError("recovery journal already exists")
        state = FixtureState(marker=self.marker)
        save_state(self.state_path, state)
        admin = self.call("GET", "/v1/auth/me", self.token(plane="business"), plane="business")
        member = self.call("GET", "/v1/auth/me", self.token(True, "business"), plane="business")
        state.organization_id = canonical(admin.get("org_id"))
        state.admin_id = canonical(admin.get("user_id"))
        state.member_id = canonical(member.get("user_id"))
        if member.get("org_id") != state.organization_id or state.admin_id == state.member_id:
            raise FixtureError("fixture identities do not share the expected organization")
        # Collision probe must not write recovered identifiers to this journal:
        # an already-existing exact marker is not ours to clean up.
        data = self.database.inventory(state.organization_id, self.model_id)
        names = {
            f"{self.marker}-{suffix}"
            for suffix in (
                "source",
                "source-v1",
                "source-v2",
                "template",
                "copy-a",
                "copy-b",
                "run-v1",
                "run-v2",
                "denied",
            )
        }
        if (
            any(
                r.get("name", r.get("title")) in names
                for k in ("agent", "workflow", "template", "conversation")
                for r in data[k]
            )
            or data["model_grants"]
        ):
            raise FixtureError("ownership marker collision")
        state.original_capabilities = sorted(data["capabilities"])
        state.original_model_grants = data["model_grants"]
        save_state(self.state_path, state)
        token = self.token()
        for version, temperature in (("v1", 0.1), ("v2", 0.7)):
            # Exact expected names + persisted org/author are a durable intent
            # even when the POST commits but its response is lost.
            save_state(self.state_path, state)
            created = self.call(
                "POST",
                "/v1/agent-definitions",
                token,
                {
                    "name": f"{self.marker}-source-{version}",
                    "description": "Owned template browser acceptance source",
                    "modelId": self.model_id,
                    "systemPrompt": f"Deterministic template source configuration {version}.",
                    "temperature": temperature,
                    "skillIds": [],
                    "knowledgeBaseIds": [],
                },
                expected=(201,),
            )
            state.source_agent_ids[version] = canonical(created.get("id"))
            save_state(self.state_path, state)
        agent = state.source_agent_ids["v1"]
        save_state(self.state_path, state)
        created = self.call(
            "POST",
            "/v1/admin/workflows",
            token,
            {
                "name": f"{self.marker}-source",
                "description": "Owned template source graph",
                "supervisorAgentDefinitionId": agent,
                "maxParallelism": 2,
                "autoRoutable": False,
                "nodes": [
                    {
                        "nodeKey": key,
                        "displayName": key,
                        "agentDefinitionId": agent,
                        "instruction": f"MULTI_AGENT_NODE:{key}",
                        "requiresConfirmation": key == "alpha",
                        "positionX": index * 240,
                        "positionY": 0,
                    }
                    for index, key in enumerate(("alpha", "beta"))
                ],
                "edges": [
                    {"sourceNodeKey": "alpha", "targetNodeKey": "beta", "condition": "on_success"}
                ],
            },
            expected=(201,),
        )
        state.source_workflow_id = canonical(created.get("id"))
        state.prepared = True
        save_state(self.state_path, state)

    def discover(self, state, data):
        owned = {key: [] for key in ("agent", "workflow", "template", "conversation", "run")}

        def identity(r, member=False):
            canonical(r.get("id"))
            if r.get("org_id") != state.organization_id or r.get(
                "user_id" if member else "created_by"
            ) != (state.member_id if member else state.admin_id):
                raise FixtureError("resource ownership mismatch")

        exact = {
            "template": {f"{self.marker}-template"},
            "workflow": {f"{self.marker}-source", f"{self.marker}-copy-a", f"{self.marker}-copy-b"},
            "conversation": {
                f"{self.marker}-run-v1",
                f"{self.marker}-run-v2",
                f"{self.marker}-run-v3",
                f"{self.marker}-denied",
            },
        }
        for kind, names in exact.items():
            for r in data[kind]:
                if r.get("title" if kind == "conversation" else "name") in names:
                    identity(r, kind == "conversation")
                    if kind == "conversation" and r.get("has_attachments"):
                        raise FixtureError(
                            "unexpected conversation attachments; no metadata cleanup fallback"
                        )
                    owned[kind].append(r)
        template_ids = {r["id"] for r in owned["template"]} | set(state.template_ids)
        workflow_ids = {r["id"] for r in owned["workflow"]}
        source_ids = set(state.source_agent_ids.values())
        for r in data["agent"]:
            if r.get("name") in {f"{self.marker}-source-v1", f"{self.marker}-source-v2"}:
                identity(r)
                key = r["name"].rsplit("-", 1)[1]
                state.source_agent_ids[key] = r["id"]
                source_ids.add(r["id"])
                owned["agent"].append(r)
        agents = {r["id"]: r for r in data["agent"]}
        for w in owned["workflow"]:
            source = w["name"] == f"{self.marker}-source"
            if source:
                if w.get("source_template_id") is not None or not set(w["agent_ids"]) <= source_ids:
                    raise FixtureError("source graph ownership mismatch")
                state.source_workflow_id = w["id"]
            elif w.get("source_template_id") not in template_ids or w.get(
                "source_template_version"
            ) not in {1, 2}:
                raise FixtureError("copy provenance mismatch")
            for id in w["agent_ids"]:
                r = agents.get(id)
                if r is None:
                    raise FixtureError("missing graph agent")
                identity(r)
                if not source:
                    if id in source_ids:
                        raise FixtureError("copy references source agent")
                    state.agent_proofs[id] = {
                        "name": r["name"],
                        "workflow_id": w["id"],
                        "template_id": w["source_template_id"],
                        "template_version": w["source_template_version"],
                    }
                if r not in owned["agent"]:
                    owned["agent"].append(r)
        # Resume an agent deletion after its graph was already removed. Exact
        # metadata and prior graph proof are both required, never an ID alone.
        for id, proof in state.agent_proofs.items():
            if id in agents and agents[id] not in owned["agent"]:
                r = agents[id]
                identity(r)
                if r["name"] != proof["name"]:
                    raise FixtureError("saved graph agent ownership mismatch")
                owned["agent"].append(r)
        agent_ids = {r["id"] for r in owned["agent"]}
        conversations = {r["id"] for r in owned["conversation"]}
        for w in data["workflow"]:
            if w["id"] not in workflow_ids and (
                set(w["agent_ids"]) & agent_ids or w.get("source_template_id") in template_ids
            ):
                raise FixtureError("foreign workflow reference blocks cleanup")
        for r in data["run"]:
            if (
                r["workflow_definition_id"] in workflow_ids
                or r["conversation_id"] in conversations
                or set(r.get("agent_ids", [])) & agent_ids
            ):
                if (
                    r["org_id"] != state.organization_id
                    or r["initiating_user_id"] != state.member_id
                    or r["conversation_id"] not in conversations
                    or r["workflow_definition_id"] not in workflow_ids
                ):
                    raise FixtureError("foreign run reference blocks cleanup")
                canonical(r["id"])
                owned["run"].append(r)
        # Recorded IDs do not bypass exact discovery (including renamed rows).
        saved = {
            "workflow": state.workflow_ids
            + ([state.source_workflow_id] if state.source_workflow_id else []),
            "template": state.template_ids,
            "agent": state.agent_ids + list(state.source_agent_ids.values()),
            "conversation": state.conversation_ids,
            "run": state.workflow_run_ids,
        }
        for kind, ids in saved.items():
            current = {r["id"] for r in data[kind]}
            proven = {r["id"] for r in owned[kind]}
            if set(ids) & current - proven:
                raise FixtureError("recorded resource ownership mismatch")
        state.template_ids = sorted(set(state.template_ids) | {r["id"] for r in owned["template"]})
        state.workflow_ids = sorted(
            set(state.workflow_ids) | (workflow_ids - {state.source_workflow_id})
        )
        state.agent_ids = sorted(set(state.agent_ids) | (agent_ids - source_ids))
        state.conversation_ids = sorted(set(state.conversation_ids) | conversations)
        state.workflow_run_ids = sorted(
            set(state.workflow_run_ids) | {r["id"] for r in owned["run"]}
        )
        return owned

    def record(self, kind, id):
        canonical(id)
        if kind not in {"template", "workflow", "agent", "conversation"}:
            raise FixtureError("invalid record kind")
        state = self._state()
        owned = self.discover(state, self.database.inventory(state.organization_id, self.model_id))
        if id not in {r["id"] for r in owned[kind]}:
            raise FixtureError("record resource ownership mismatch")
        save_state(self.state_path, state)

    def _owned_conversation(self, conversation_id):
        canonical(conversation_id)
        state = self._state()
        owned = self.discover(state, self.database.inventory(state.organization_id, self.model_id))
        if conversation_id not in {r["id"] for r in owned["conversation"]}:
            raise FixtureError("resource is not a recorded owned conversation")
        save_state(self.state_path, state)

    def stop_worker(self, conversation_id: str) -> None:
        self._owned_conversation(conversation_id)
        self.runner_cleaner.stop_owned(conversation_id)

    def start_worker(self, conversation_id: str) -> None:
        self._owned_conversation(conversation_id)
        self.runner_cleaner.start_owned(conversation_id)

    def restore(self, state):
        errors = []
        operations = []
        if state.original_model_grants is not None:
            operations.append(
                (
                    "/v1/admin/authorization/grants/reconcile",
                    dict(
                        resourceType="model",
                        resourceId=self.model_id,
                        grants=state.original_model_grants,
                    ),
                )
            )
        if state.original_capabilities is not None:
            operations.append(
                (
                    "/v1/admin/authorization/capabilities/organization",
                    dict(capabilities=state.original_capabilities),
                )
            )
        for path, body in operations:
            try:
                response = self.call(
                    "PUT",
                    path,
                    self.token(),
                    body,
                    expected=(200, 404) if "grants" in path else (200,),
                )
                if response == {"_httpStatus": 404}:
                    # Recovery can follow overlay removal: the virtual catalog
                    # resource is gone but its exact-owned grant rows may remain.
                    self.database.restore_model_grants(state, self.model_id)
            except Exception:
                errors.append("permission restoration failed")
        return errors

    def cleanup(self):
        if not self.state_path.exists():
            return
        state = self._state()
        if not state.organization_id:
            return
        errors = []
        try:
            owned = self.discover(
                state, self.database.inventory(state.organization_id, self.model_id)
            )
            save_state(self.state_path, state)
            self.recover_temporal(state, close=True)
            for c in owned["conversation"]:
                self.call(
                    "DELETE",
                    f"/v1/conversations/{c['id']}",
                    self.token(True, "business"),
                    plane="business",
                    expected=(204, 404),
                )
                self.runner_cleaner.remove_owned(c["id"])
            # Retry previously removed conversations' runners too.
            for id in set(state.conversation_ids) - {r["id"] for r in owned["conversation"]}:
                self.runner_cleaner.remove_owned(id)

            def remove_rows(**kinds):
                batch = {key: [] for key in owned}
                batch.update(kinds)
                self.database.delete_owned(state, batch)

            remove_rows(
                conversation=owned["conversation"],
                run=owned["run"],
                # Reference-only ownership evidence includes source runs too;
                # the source definition must survive until its template is gone.
                run_definition=owned["workflow"],
                workflow=[w for w in owned["workflow"] if w["id"] != state.source_workflow_id],
            )
            for r in owned["agent"]:
                if r["id"] in state.agent_ids:
                    self.delete_agent(state, r)
            remove_rows(template=owned["template"])
            remove_rows(
                workflow=[w for w in owned["workflow"] if w["id"] == state.source_workflow_id]
            )
            for r in owned["agent"]:
                if r["id"] in state.source_agent_ids.values():
                    self.delete_agent(state, r)
        except Exception as exc:
            errors.append(
                str(exc)
                if isinstance(exc, FixtureError)
                else "owned cleanup failed; journal retained"
            )
        errors.extend(self.restore(state))
        if errors:
            raise FixtureError("; ".join(errors))

    def delete_agent(self, state, row):
        current = self.database.inventory(state.organization_id, self.model_id)
        if any(row["id"] in w["agent_ids"] for w in current["workflow"]) or any(
            row["id"] in r.get("agent_ids", []) for r in current["run"]
        ):
            raise FixtureError("foreign agent reference blocks deletion")
        match = next((a for a in current["agent"] if a["id"] == row["id"]), None)
        if match is not None and match != row:
            raise FixtureError("agent ownership changed before deletion")
        if match:
            self.call(
                "DELETE", f"/v1/agent-definitions/{row['id']}", self.token(), expected=(204, 404)
            )

    def verify_clean(self):
        if not self.state_path.exists():
            return
        state = self._state()
        if not state.organization_id:
            return
        self.recover_temporal(state, close=False)
        data = self.database.inventory(state.organization_id, self.model_id)
        owned = self.discover(state, data)
        if any(owned.values()):
            raise FixtureError("owned resources remain")
        if state.original_capabilities is not None and sorted(data["capabilities"]) != sorted(
            state.original_capabilities
        ):
            raise FixtureError("capability restoration mismatch")
        if state.original_model_grants is not None and sorted(
            map(lambda r: json.dumps(r, sort_keys=True), data["model_grants"])
        ) != sorted(map(lambda r: json.dumps(r, sort_keys=True), state.original_model_grants)):
            raise FixtureError("grant restoration mismatch")
        for id in state.conversation_ids:
            self.runner_cleaner.assert_absent(id)

    def recover_temporal(self, state, *, close):
        """Prevalidate all owned trees, then close exact runs before any deletion.

        No visibility/list query, prefix adoption, cancellation requiring a live
        worker, history deletion, or permission snapshot. Existing private UUID
        evidence is mandatory even for recovery after old DB cleanup.
        """
        try:
            trees = []
            for conversation in state.conversation_ids:
                root = self.temporal_transport.read(f"conversation-{conversation}")
                run = root["run_id"] if root else None
                if conversation in state.temporal_root_run_ids:
                    if state.temporal_root_run_ids[conversation] != run:
                        raise FixtureError("Temporal root execution changed or history missing")
                tree = self.temporal_tree(state, conversation, root) if root else []
                state.temporal_root_run_ids[conversation] = run
                trees.append((conversation, root, tree))
            # A missing root may belong to an API-created conversation with no
            # turn. Every recorded platform run must then be accounted for by
            # another proven tree; missing established history is ambiguous.
            found_runs = {
                r["input"]["run_id"]
                for _, _, tree in trees
                for r in tree
                if r["workflow_type"] == "MultiAgentWorkflow"
            }
            if (
                any(root is None for _, root, _ in trees)
                and set(state.workflow_run_ids) - found_runs
            ):
                raise FixtureError("Temporal established root history unavailable")
            if close:
                save_state(self.state_path, state)
            for conversation, root, tree in trees:
                if not root:
                    continue
                if close:
                    # Parents first: freeze child creation before inspecting
                    # their final history. Rewalk catches children started
                    # between prevalidation and server-side termination.
                    for _ in range(4):
                        running = [r for r in tree if r["status"] == "RUNNING"]
                        if not running:
                            break
                        target = running[0]
                        self.temporal_transport.terminate(target["workflow_id"], target["run_id"])
                        tree = self.temporal_tree(state, conversation, root)
                    # Up to root + orchestrator + two simultaneous C5 nodes.
                if any(r["status"] == "RUNNING" for r in tree):
                    raise FixtureError("Temporal owned execution remains open")
        except FixtureError:
            raise
        except Exception as exc:
            raise FixtureError("Temporal closure unavailable; journal retained") from exc

    def temporal_tree(self, state, conversation, root):
        rows = []
        seen = set()
        limit = self.max_temporal_executions
        if type(limit) is not int or not 1 <= limit <= 64:
            raise FixtureError("Temporal execution graph bound invalid")

        def visit(id, run, parent=None):
            if (id, run) in seen or len(seen) >= limit:
                raise FixtureError("Temporal unexpected execution graph")
            seen.add((id, run))
            canonical(run)
            r = self.temporal_transport.read(id, run)
            if (
                not r
                or r["workflow_id"] != id
                or r["run_id"] != run
                or r["task_queue"] != f"session-{conversation}"
                or r["parent"] != parent
                or r["continued"]
                or r["pending_children"]
                or r["status"]
                not in {"RUNNING", "COMPLETED", "FAILED", "CANCELED", "TERMINATED", "TIMED_OUT"}
            ):
                raise FixtureError("Temporal execution identity or ancestry mismatch")
            inputs = r["input"]
            if parent is None:
                valid = (
                    id == f"conversation-{conversation}"
                    and r["workflow_type"] == "ConversationWorkflow"
                    and inputs.get("conversation_id") == conversation
                )
            elif r["workflow_type"] == "EvaluationWorkflow":
                turn = canonical(inputs.get("turn_id"))
                valid = (
                    id == f"{conversation}-eval-{turn}"
                    and inputs.get("conversation_id") == conversation
                    and parent["workflow_id"] == f"conversation-{conversation}"
                    and not r["children"]
                )
            else:
                platform_run, _, suffix = id.partition(":")
                valid = platform_run in state.workflow_run_ids
                if suffix == "orchestrator":
                    valid = (
                        valid
                        and r["workflow_type"] == "MultiAgentWorkflow"
                        and inputs.get("run_id") == platform_run
                        and parent["workflow_id"] == f"conversation-{conversation}"
                    )
                else:
                    valid = (
                        valid
                        and r["workflow_type"] == "AgentWorkflow"
                        and inputs.get("workflow_run_id") == platform_run
                        and parent["workflow_id"] == f"{platform_run}:orchestrator"
                        and suffix
                        in {
                            "supervisor_prepare",
                            "supervisor_synthesize",
                            "node:alpha",
                            "node:start",
                            "node:draft",
                            "node:review",
                            "node:beta",
                            "node:gamma",
                            "node:delta",
                            "node:epsilon",
                            "node:zeta",
                        }
                        and inputs.get("workflow_phase")
                        == ("node" if suffix.startswith("node:") else suffix)
                        and (
                            not suffix.startswith("node:")
                            or inputs.get("workflow_node_key") == suffix[5:]
                        )
                    )
            if not valid:
                raise FixtureError("Temporal execution is not journal-owned")
            rows.append(r)
            for child in r["children"]:
                if type(child["initiated_event_id"]) is not int or child["initiated_event_id"] <= 0:
                    raise FixtureError("Temporal child initiation identity invalid")
                visit(
                    child["workflow_id"],
                    child["run_id"],
                    {
                        "workflow_id": id,
                        "run_id": run,
                        "initiated_event_id": child["initiated_event_id"],
                    },
                )

        visit(root["workflow_id"], root["run_id"])
        return rows


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "record", "stop-worker", "start-worker", "cleanup", "verify-clean"):
        p = sub.add_parser(name)
        p.add_argument("--state", required=True, type=Path)
        p.add_argument("--marker", required=True)
        if name == "record":
            p.add_argument(
                "--kind", choices=("template", "workflow", "agent", "conversation"), required=True
            )
            p.add_argument("--id", required=True)
        elif name in {"stop-worker", "start-worker"}:
            p.add_argument("--conversation", required=True)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        controller = FixtureController(Config(), args.state, args.marker)
        if args.command == "record":
            controller.record(args.kind, args.id)
        elif args.command in {"stop-worker", "start-worker"}:
            getattr(controller, args.command.replace("-", "_"))(args.conversation)
        else:
            getattr(controller, args.command.replace("-", "_"))()
    except Exception as exc:
        detail = str(exc) if isinstance(exc, FixtureError) else "operation failed"
        print(f"[workflow-templates-ui-e2e] {detail}; preserve recovery journal", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
