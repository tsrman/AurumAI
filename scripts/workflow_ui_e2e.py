#!/usr/bin/env python3
"""Owned fixture lifecycle for the Compose workflow browser acceptance."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Literal


OWNED_MARKER = re.compile(r"^workflow-ui-e2e-[a-f0-9]{10,32}$")


class FixtureError(RuntimeError):
    """Safe acceptance failure that never contains credentials or payloads."""


@dataclass(frozen=True)
class Config:
    business_url: str = field(
        default_factory=lambda: os.getenv(
            "E2E_BUSINESS_BACKEND_URL", "http://localhost:8000"
        ).rstrip("/")
    )
    admin_url: str = field(
        default_factory=lambda: os.getenv(
            "E2E_ADMIN_BACKEND_URL", "http://localhost:8001"
        ).rstrip("/")
    )
    keycloak_url: str = field(
        default_factory=lambda: os.getenv(
            "E2E_KEYCLOAK_URL", "http://localhost:8080"
        ).rstrip("/")
    )
    realm: str = field(
        default_factory=lambda: os.getenv("E2E_KEYCLOAK_REALM", "aurum-ai")
    )
    model: str = field(
        default_factory=lambda: os.getenv("E2E_WORKFLOW_UI_MODEL", "gpt-4o-mini")
    )
    postgres_container: str = field(
        default_factory=lambda: os.getenv(
            "E2E_POSTGRES_CONTAINER", "aurum-ai-postgres"
        )
    )


@dataclass
class FixtureState:
    marker: str
    organization_id: str = ""
    member_id: str = ""
    original_capabilities: list[str] | None = None
    original_model_grants: list[dict[str, str | None]] | None = None
    agent_ids: dict[str, str] = field(default_factory=dict)
    workflow_names: list[str] = field(default_factory=list)
    conversation_ids: list[str] = field(default_factory=list)


def _canonical_uuid(value: str, label: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise FixtureError(f"invalid {label} UUID") from exc


def validate_owned_state(state: FixtureState, *, require_resources: bool) -> None:
    if not OWNED_MARKER.fullmatch(state.marker):
        raise FixtureError("invalid workflow browser ownership marker")
    if state.organization_id:
        _canonical_uuid(state.organization_id, "organization")
    elif require_resources:
        raise FixtureError("missing organization UUID for destructive cleanup")
    if state.member_id:
        _canonical_uuid(state.member_id, "member")
    for agent_id in state.agent_ids.values():
        _canonical_uuid(agent_id, "agent")
    for conversation_id in state.conversation_ids:
        _canonical_uuid(conversation_id, "conversation")
    expected_workflow_names = {
        f"{state.marker}-original",
        f"{state.marker}-renamed",
    }
    if any(name not in expected_workflow_names for name in state.workflow_names):
        raise FixtureError("invalid owned workflow name")
    if require_resources and not (
        state.agent_ids or state.workflow_names or state.conversation_ids
    ):
        raise FixtureError("missing owned resource identifiers")


def save_state(path: Path, state: FixtureState) -> None:
    validate_owned_state(state, require_resources=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(asdict(state), stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def load_state(path: Path) -> FixtureState:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError("workflow browser state is unreadable") from exc
    if not isinstance(payload, dict):
        raise FixtureError("workflow browser state must be an object")
    allowed = {item.name for item in fields(FixtureState)}
    unknown = set(payload) - allowed
    if unknown:
        raise FixtureError("unknown state field")
    try:
        state = FixtureState(**payload)
    except TypeError as exc:
        raise FixtureError("workflow browser state has invalid fields") from exc
    validate_owned_state(state, require_resources=False)
    return state


class ApiClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    def token(self, username: str, password: str, client_id: str) -> str:
        data = urllib.parse.urlencode(
            {
                "grant_type": "password",
                "client_id": client_id,
                "username": username,
                "password": password,
            }
        ).encode()
        request = urllib.request.Request(
            f"{self.config.keycloak_url}/realms/{self.config.realm}/protocol/openid-connect/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise FixtureError("Keycloak token acquisition failed") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise FixtureError("Keycloak token acquisition failed")
        return token

    def request(
        self,
        plane: Literal["business", "admin"],
        method: str,
        path: str,
        token: str,
        *,
        body: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        base_url = self.config.business_url if plane == "business" else self.config.admin_url
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = urllib.request.Request(
            f"{base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
        except OSError as exc:
            raise FixtureError(f"{method} {path}: transport failed") from exc
        if status not in expected:
            raise FixtureError(f"{method} {path}: expected {expected}, got {status}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FixtureError(f"{method} {path}: invalid JSON response") from exc


class Database:
    def __init__(self, config: Config) -> None:
        self.config = config

    def _run(self, sql: str) -> str:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                self.config.postgres_container,
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
                "-At",
                "-U",
                "postgres",
                "-d",
                "aurum_ai",
            ],
            input=sql,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise FixtureError("owned database operation failed")
        return result.stdout.strip()

    def query_values(self, sql: str) -> list[str]:
        output = self._run(sql)
        return [line for line in output.splitlines() if line]

    def execute(self, sql: str) -> None:
        self._run(sql)


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class DockerRunnerCleaner:
    def __init__(self, *, run: RunCommand = subprocess.run) -> None:
        self.run = run

    def remove_owned(self, conversation_id: str) -> None:
        canonical_id = _canonical_uuid(conversation_id, "conversation")
        result = self.run(
            ["docker", "inspect", f"runner-{canonical_id}"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.lower()
            if "no such object" in detail or "no such container" in detail:
                return
            raise FixtureError("runner ownership inspection failed")
        try:
            documents = json.loads(result.stdout)
            if not isinstance(documents, list) or len(documents) != 1:
                raise ValueError
            document = documents[0]
            config = document["Config"]
            labels = config["Labels"]
            environment = config["Env"]
            container_id = document["Id"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FixtureError("runner ownership inspection is invalid") from exc
        if (
            not isinstance(labels, dict)
            or labels.get("aurum-ai.component") != "conversation-runner"
            or not isinstance(environment, list)
            or f"CONVERSATION_ID={canonical_id}" not in environment
            or not isinstance(container_id, str)
            or not container_id
        ):
            raise FixtureError("runner ownership could not be proven")
        removed = self.run(
            ["docker", "rm", "-f", container_id],
            text=True,
            capture_output=True,
            check=False,
        )
        if removed.returncode != 0:
            raise FixtureError("owned runner removal failed")


def _response_data(response: Any, label: str) -> Any:
    if not isinstance(response, dict) or "data" not in response:
        raise FixtureError(f"{label}: invalid API response")
    return response["data"]


def _grant_specs(grants: list[dict[str, str | None]]) -> list[dict[str, str]]:
    specs: list[dict[str, str]] = []
    for grant in grants:
        principal_type = grant.get("principalType")
        principal_id = grant.get("principalId")
        permission = grant.get("permission")
        if not all(
            isinstance(value, str) and value
            for value in (principal_type, principal_id, permission)
        ):
            raise FixtureError("saved model grant is invalid")
        specs.append(
            {
                "principalType": principal_type,
                "principalId": _canonical_uuid(principal_id, "grant principal"),
                "permission": permission,
            }
        )
    return specs


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class Acceptance:
    def __init__(
        self,
        config: Config,
        state_path: Path,
        marker: str,
        *,
        api: Any | None = None,
        database: Any | None = None,
        runner_cleaner: Any | None = None,
    ) -> None:
        self.config = config
        self.state_path = state_path
        self.marker = marker
        self.api = api or ApiClient(config)
        self.database = database or Database(config)
        self.runner_cleaner = runner_cleaner or DockerRunnerCleaner()

    def _state(self) -> FixtureState:
        state = load_state(self.state_path)
        if state.marker != self.marker:
            raise FixtureError("state marker does not match command marker")
        return state

    def _save(self, state: FixtureState) -> None:
        save_state(self.state_path, state)

    def prepare(self) -> None:
        state = self._state()
        validate_owned_state(state, require_resources=False)
        if state.organization_id or state.agent_ids or state.original_capabilities is not None:
            raise FixtureError("workflow browser fixture is already prepared")

        admin_business_token = self.api.token("admin", "admin", "business-ui")
        admin_admin_token = self.api.token("admin", "admin", "admin-ui")
        member_business_token = self.api.token("user", "user", "business-ui")
        self.api.request("business", "GET", "/v1/auth/me", admin_business_token)
        self.api.request("business", "GET", "/v1/auth/me", member_business_token)

        subjects = _response_data(
            self.api.request(
                "admin",
                "GET",
                "/v1/admin/authorization/subjects",
                admin_admin_token,
            ),
            "authorization subjects",
        )
        if not isinstance(subjects, list):
            raise FixtureError("authorization subjects: invalid API response")
        organizations = [
            item
            for item in subjects
            if isinstance(item, dict) and item.get("principalType") == "organization"
        ]
        members = [
            item
            for item in subjects
            if isinstance(item, dict)
            and item.get("principalType") == "user"
            and item.get("label") == "Regular User"
        ]
        if len(organizations) != 1 or len(members) != 1:
            raise FixtureError("authorization subjects are not provisioned as expected")
        state.organization_id = _canonical_uuid(
            str(organizations[0].get("principalId", "")), "organization"
        )
        state.member_id = _canonical_uuid(
            str(members[0].get("principalId", "")), "member"
        )
        collisions = self.database.query_values(self._collision_query(state))
        if collisions:
            raise FixtureError("workflow browser ownership marker is already in use")
        state.workflow_names = [f"{state.marker}-original", f"{state.marker}-renamed"]
        self._save(state)

        capability_data = _response_data(
            self.api.request(
                "admin",
                "GET",
                "/v1/admin/authorization/capabilities/organization",
                admin_admin_token,
            ),
            "organization capabilities",
        )
        if not isinstance(capability_data, dict) or not isinstance(
            capability_data.get("capabilities"), list
        ):
            raise FixtureError("organization capabilities: invalid API response")
        state.original_capabilities = [str(item) for item in capability_data["capabilities"]]
        self._save(state)
        if "chat.send" not in state.original_capabilities:
            raise FixtureError("organization must already have chat.send capability")
        capabilities = sorted(set(state.original_capabilities) | {"workspace.agents.create"})
        self.api.request(
            "admin",
            "PUT",
            "/v1/admin/authorization/capabilities/organization",
            admin_admin_token,
            body={"capabilities": capabilities},
        )

        encoded_model = urllib.parse.quote(self.config.model, safe="")
        model_grants = _response_data(
            self.api.request(
                "admin",
                "GET",
                "/v1/admin/authorization/grants"
                f"?resourceType=model&resourceId={encoded_model}",
                admin_admin_token,
            ),
            "model grants",
        )
        if not isinstance(model_grants, list) or not all(
            isinstance(item, dict) for item in model_grants
        ):
            raise FixtureError("model grants: invalid API response")
        state.original_model_grants = [dict(item) for item in model_grants]
        self._save(state)

        models = _response_data(
            self.api.request("business", "GET", "/v1/models", member_business_token),
            "visible models",
        )
        if not isinstance(models, list):
            raise FixtureError("visible models: invalid API response")
        visible = {item.get("id") for item in models if isinstance(item, dict)}
        if self.config.model not in visible:
            grants = _grant_specs(state.original_model_grants)
            grants.append(
                {
                    "principalType": "user",
                    "principalId": state.member_id,
                    "permission": "read",
                }
            )
            self.api.request(
                "admin",
                "PUT",
                "/v1/admin/authorization/grants/reconcile",
                admin_admin_token,
                body={
                    "resourceType": "model",
                    "resourceId": self.config.model,
                    "grants": grants,
                },
            )

        for key in ("supervisor", "alpha", "beta"):
            created = _response_data(
                self.api.request(
                    "admin",
                    "POST",
                    "/v1/agent-definitions",
                    admin_admin_token,
                    body={
                        "name": f"{state.marker}-{key}",
                        "description": "owned workflow browser acceptance fixture",
                        "modelId": self.config.model,
                        "systemPrompt": f"Deterministic {key} workflow browser fixture.",
                        "temperature": 0,
                        "skillIds": [],
                        "knowledgeBaseIds": [],
                    },
                    expected=(201,),
                ),
                f"create {key} agent",
            )
            if not isinstance(created, dict) or not isinstance(created.get("id"), str):
                raise FixtureError(f"create {key} agent: invalid API response")
            state.agent_ids[key] = _canonical_uuid(created["id"], "agent")
            self._save(state)
            self.api.request(
                "admin",
                "PUT",
                "/v1/admin/authorization/grants/reconcile",
                admin_admin_token,
                body={
                    "resourceType": "agent_definition",
                    "resourceId": state.agent_ids[key],
                    "grants": [
                        {
                            "principalType": "user",
                            "principalId": state.member_id,
                            "permission": "read",
                        }
                    ],
                },
            )

    def _collision_query(self, state: FixtureState) -> str:
        org_id = _canonical_uuid(state.organization_id, "organization")
        names = [
            *(f"{state.marker}-{key}" for key in ("supervisor", "alpha", "beta")),
            f"{state.marker}-original",
            f"{state.marker}-renamed",
        ]
        rendered = ",".join(_sql_text(name) for name in names)
        return (
            "SELECT 'agent:' || name FROM agent_definitions "
            f"WHERE org_id='{org_id}'::uuid AND name IN ({rendered}) UNION ALL "
            "SELECT 'workflow:' || name FROM workflow_definitions "
            f"WHERE org_id='{org_id}'::uuid AND name IN ({rendered}) UNION ALL "
            "SELECT 'conversation:' || id::text FROM conversations "
            f"WHERE org_id='{org_id}'::uuid AND title={_sql_text(state.marker)};"
        )

    def _conversation_query(self, state: FixtureState) -> str:
        org_id = _canonical_uuid(state.organization_id, "organization")
        return (
            "SELECT id::text FROM conversations "
            f"WHERE org_id='{org_id}'::uuid AND title={_sql_text(state.marker)};"
        )

    @staticmethod
    def _owned_turn_cleanup(
        org_id: str, conversation_ids: list[str], workflow_predicate: str
    ) -> list[str]:
        # Turns outlive conversation rows (they cascade from conversation_identities)
        # and restrict workflow_run deletion, so remove only turns of this fixture's
        # conversations that are standalone or belong to its own workflow runs.
        if not conversation_ids:
            return []
        rendered = ",".join(f"'{item}'::uuid" for item in conversation_ids)
        return [
            f"DELETE FROM interaction_turns WHERE org_id='{org_id}'::uuid "
            f"AND conversation_id IN ({rendered}) AND (workflow_run_id IS NULL OR "
            "workflow_run_id IN (SELECT wr.id FROM workflow_runs wr JOIN "
            "workflow_definitions wd ON wd.id=wr.workflow_definition_id "
            f"AND wd.org_id=wr.org_id WHERE wd.org_id='{org_id}'::uuid "
            f"AND wd.{workflow_predicate}))"
        ]

    def _cleanup_sql(self, state: FixtureState) -> str:
        org_id = _canonical_uuid(state.organization_id, "organization")
        marker = _sql_text(state.marker)
        workflow_names = ",".join(_sql_text(name) for name in state.workflow_names)
        workflow_predicate = f"name IN ({workflow_names})"
        agent_names = ",".join(
            _sql_text(f"{state.marker}-{key}")
            for key in ("supervisor", "alpha", "beta")
        )
        agent_ids = sorted(
            {_canonical_uuid(item, "agent") for item in state.agent_ids.values()}
        )
        rendered_agent_ids = ",".join(f"'{item}'::uuid" for item in agent_ids)
        agent_predicate = f"name IN ({agent_names})"
        if rendered_agent_ids:
            agent_predicate = f"(id IN ({rendered_agent_ids}) OR {agent_predicate})"
        conversation_ids = sorted(
            {_canonical_uuid(item, "conversation") for item in state.conversation_ids}
        )
        conversation_predicate = f"title={marker}"
        if conversation_ids:
            rendered = ",".join(f"'{item}'::uuid" for item in conversation_ids)
            conversation_predicate = f"(title={marker} OR id IN ({rendered}))"
        statements = [
            (
                "DELETE FROM access_grants "
                f"WHERE org_id='{org_id}'::uuid AND ((resource_type='workflow' "
                "AND resource_id IN (SELECT id::text FROM workflow_definitions "
                f"WHERE org_id='{org_id}'::uuid AND {workflow_predicate})) OR "
                "(resource_type='agent_definition' AND resource_id IN "
                "(SELECT id::text FROM agent_definitions "
                f"WHERE org_id='{org_id}'::uuid AND {agent_predicate})))"
            ),
            (
                "UPDATE llm_usage_events SET workflow_run_id=NULL, workflow_phase=NULL, "
                f"workflow_node_key=NULL WHERE org_id='{org_id}'::uuid AND "
                "workflow_run_id IN (SELECT wr.id FROM workflow_runs wr JOIN "
                "workflow_definitions wd ON wd.id=wr.workflow_definition_id "
                f"AND wd.org_id=wr.org_id WHERE wd.org_id='{org_id}'::uuid "
                f"AND wd.{workflow_predicate})"
            ),
            (
                f"DELETE FROM workflow_node_runs WHERE org_id='{org_id}'::uuid AND "
                "workflow_run_id IN (SELECT wr.id FROM workflow_runs wr JOIN "
                "workflow_definitions wd ON wd.id=wr.workflow_definition_id "
                f"AND wd.org_id=wr.org_id WHERE wd.org_id='{org_id}'::uuid "
                f"AND wd.{workflow_predicate})"
            ),
            *self._owned_turn_cleanup(org_id, conversation_ids, workflow_predicate),
            (
                f"DELETE FROM workflow_runs WHERE org_id='{org_id}'::uuid AND "
                "workflow_definition_id IN (SELECT id FROM workflow_definitions "
                f"WHERE org_id='{org_id}'::uuid AND {workflow_predicate})"
            ),
            (
                f"DELETE FROM conversations WHERE org_id='{org_id}'::uuid "
                f"AND {conversation_predicate}"
            ),
            (
                f"DELETE FROM workflow_definitions WHERE org_id='{org_id}'::uuid "
                f"AND {workflow_predicate}"
            ),
            (
                f"DELETE FROM agent_definitions WHERE org_id='{org_id}'::uuid "
                f"AND {agent_predicate}"
            ),
        ]
        return ";\n".join(statements) + ";"

    def _restore_sql(self, state: FixtureState) -> str:
        org_id = _canonical_uuid(state.organization_id, "organization")
        statements: list[str] = []
        if state.original_capabilities is not None:
            statements.append(
                f"DELETE FROM organization_capabilities WHERE org_id='{org_id}'::uuid"
            )
            for capability in state.original_capabilities:
                statements.append(
                    "INSERT INTO organization_capabilities (org_id, capability) VALUES "
                    f"('{org_id}'::uuid, {_sql_text(capability)})"
                )
        if state.original_model_grants is not None:
            statements.append(
                "DELETE FROM access_grants "
                f"WHERE org_id='{org_id}'::uuid AND resource_type='model' "
                f"AND resource_id={_sql_text(self.config.model)}"
            )
            for grant in state.original_model_grants:
                grant_id = _canonical_uuid(str(grant.get("id", "")), "grant")
                principal_id = _canonical_uuid(
                    str(grant.get("principalId", "")), "grant principal"
                )
                resource_type = str(grant.get("resourceType", ""))
                resource_id = str(grant.get("resourceId", ""))
                principal_type = str(grant.get("principalType", ""))
                permission = str(grant.get("permission", ""))
                created_via = str(grant.get("createdVia", ""))
                created_by_raw = grant.get("createdBy")
                created_by = (
                    "NULL"
                    if created_by_raw is None
                    else f"'{_canonical_uuid(str(created_by_raw), 'grant creator')}'::uuid"
                )
                statements.append(
                    "INSERT INTO access_grants (id, org_id, resource_type, resource_id, "
                    "principal_type, principal_id, permission, created_by, created_via) VALUES "
                    f"('{grant_id}'::uuid, '{org_id}'::uuid, {_sql_text(resource_type)}, "
                    f"{_sql_text(resource_id)}, {_sql_text(principal_type)}, "
                    f"'{principal_id}'::uuid, {_sql_text(permission)}, {created_by}, "
                    f"{_sql_text(created_via)})"
                )
        if not statements:
            return ""
        return "BEGIN;\n" + ";\n".join(statements) + ";\nCOMMIT;"

    def _restore_configuration(self, state: FixtureState) -> None:
        if state.original_capabilities is None and state.original_model_grants is None:
            return
        try:
            admin_token = self.api.token("admin", "admin", "admin-ui")
            if state.original_model_grants is not None:
                self.api.request(
                    "admin",
                    "PUT",
                    "/v1/admin/authorization/grants/reconcile",
                    admin_token,
                    body={
                        "resourceType": "model",
                        "resourceId": self.config.model,
                        "grants": _grant_specs(state.original_model_grants),
                    },
                )
            if state.original_capabilities is not None:
                self.api.request(
                    "admin",
                    "PUT",
                    "/v1/admin/authorization/capabilities/organization",
                    admin_token,
                    body={"capabilities": state.original_capabilities},
                )
        except FixtureError:
            fallback = self._restore_sql(state)
            if fallback:
                self.database.execute(fallback)

    def cleanup(self) -> None:
        if not self.state_path.exists():
            return
        state = self._state()
        validate_owned_state(state, require_resources=False)
        if not state.organization_id:
            if state.agent_ids or state.workflow_names or state.conversation_ids:
                validate_owned_state(state, require_resources=True)
            return

        errors: list[str] = []
        has_resources = bool(state.agent_ids or state.workflow_names or state.conversation_ids)
        if has_resources:
            validate_owned_state(state, require_resources=True)
            try:
                found = self.database.query_values(self._conversation_query(state))
                state.conversation_ids = sorted(
                    {
                        *state.conversation_ids,
                        *(_canonical_uuid(item, "conversation") for item in found),
                    }
                )
                self._save(state)
            except FixtureError as exc:
                errors.append(f"conversation discovery: {exc}")
            for conversation_id in state.conversation_ids:
                try:
                    self.runner_cleaner.remove_owned(conversation_id)
                except FixtureError as exc:
                    errors.append(f"runner cleanup: {exc}")
            try:
                self.database.execute(self._cleanup_sql(state))
            except FixtureError as exc:
                errors.append(f"database cleanup: {exc}")
        try:
            self._restore_configuration(state)
        except FixtureError as exc:
            errors.append(f"configuration restore: {exc}")
        if errors:
            raise FixtureError("; ".join(errors))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "cleanup"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--state", required=True, type=Path)
        subparser.add_argument("--marker", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        config = Config()
        acceptance = Acceptance(config, args.state, args.marker)
        if args.command == "prepare":
            if args.state.exists():
                existing = load_state(args.state)
                if existing.marker != args.marker:
                    raise FixtureError("state marker does not match command marker")
                raise FixtureError("workflow browser recovery state already exists")
            state = FixtureState(marker=args.marker)
            save_state(args.state, state)
            try:
                acceptance.prepare()
            except FixtureError:
                try:
                    acceptance.cleanup()
                except FixtureError as cleanup_error:
                    print(
                        f"[workflow-ui-e2e] cleanup after prepare failed: {cleanup_error}",
                        file=sys.stderr,
                    )
                raise
        else:
            acceptance.cleanup()
    except FixtureError as exc:
        print(f"[workflow-ui-e2e] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
