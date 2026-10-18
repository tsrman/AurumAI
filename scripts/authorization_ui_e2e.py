#!/usr/bin/env python3
"""Guarded fixture lifecycle for the Authorization v2 browser acceptance."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field, fields, replace
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Literal
import urllib.error
import urllib.parse
import urllib.request
import uuid


OWNED_MARKER = re.compile(r"^authorization-ui-e2e-[a-f0-9]{10,32}$")
CAPABILITY = re.compile(r"^[a-z][a-z0-9_.-]{1,99}$")


class FixtureError(RuntimeError):
    """Safe operational error that never contains response bodies or secrets."""


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
    model_id: str = field(
        default_factory=lambda: os.getenv("TEST_GATEWAY_MODEL_ID", "")
    )
    postgres_container: str = field(
        default_factory=lambda: os.getenv(
            "E2E_POSTGRES_CONTAINER", "aurum-ai-postgres"
        )
    )


@dataclass
class OwnedResource:
    id: str = ""
    name: str = ""
    initial_grants: list[dict[str, str | None]] = field(default_factory=list)


@dataclass
class FixtureState:
    marker: str
    model_id: str
    organization_id: str = ""
    organization_label: str = ""
    analysts_group_id: str = ""
    admin_id: str = ""
    user_id: str = ""
    user2_id: str = ""
    original_organization_capabilities: list[str] | None = None
    original_analysts_capabilities: list[str] | None = None
    original_model_grants: list[dict[str, str | None]] | None = None
    skill: OwnedResource = field(default_factory=OwnedResource)
    knowledge_base: OwnedResource = field(default_factory=OwnedResource)
    agent: OwnedResource = field(default_factory=OwnedResource)
    workflow: OwnedResource = field(default_factory=OwnedResource)
    conversation_ids: list[str] = field(default_factory=list)
    workflow_run_ids: list[str] = field(default_factory=list)
    prepared: bool = False


def _valid_uuid(value: str, label: str, *, required: bool = False) -> None:
    if not value:
        if required:
            raise FixtureError(f"{label} is required")
        return
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise FixtureError(f"invalid {label}") from exc
    if str(parsed) != value.lower():
        raise FixtureError(f"invalid {label}")


def _validate_capabilities(value: list[str] | None, label: str) -> None:
    if value is None:
        return
    if any(not CAPABILITY.fullmatch(item) for item in value):
        raise FixtureError(f"invalid {label}")
    if value != sorted(set(value)):
        raise FixtureError(f"{label} must be sorted and unique")


def _validate_grants(
    value: list[dict[str, str | None]] | None, label: str
) -> None:
    if value is None:
        return
    for grant in value:
        if not isinstance(grant, dict) or not all(
            isinstance(key, str) and (isinstance(item, str) or item is None)
            for key, item in grant.items()
        ):
            raise FixtureError(f"invalid {label}")


def _validate_owned_resource(
    resource: OwnedResource,
    label: str,
    expected_name: str,
    *,
    required: bool,
) -> None:
    _valid_uuid(resource.id, f"{label}_id", required=required)
    if resource.name and resource.name != expected_name:
        raise FixtureError(f"invalid {label}_name")
    if required and resource.name != expected_name:
        raise FixtureError(f"{label}_name is required")
    if resource.id and not resource.name:
        raise FixtureError(f"{label}_id requires {label}_name")
    _validate_grants(resource.initial_grants, f"{label}_initial_grants")


def validate_owned_state(
    state: FixtureState, *, require_resources: bool = False
) -> None:
    if not OWNED_MARKER.fullmatch(state.marker):
        raise FixtureError("invalid ownership marker")
    if state.model_id != f"{state.marker}-model":
        raise FixtureError("invalid model_id")

    identity_fields = (
        (state.organization_id, "organization_id"),
        (state.analysts_group_id, "analysts_group_id"),
        (state.admin_id, "admin_id"),
        (state.user_id, "user_id"),
        (state.user2_id, "user2_id"),
    )
    for value, label in identity_fields:
        _valid_uuid(value, label, required=require_resources)
    if require_resources and not state.organization_label:
        raise FixtureError("organization_label is required")

    _validate_capabilities(
        state.original_organization_capabilities,
        "original_organization_capabilities",
    )
    _validate_capabilities(
        state.original_analysts_capabilities,
        "original_analysts_capabilities",
    )
    _validate_grants(state.original_model_grants, "original_model_grants")
    if require_resources and any(
        item is None
        for item in (
            state.original_organization_capabilities,
            state.original_analysts_capabilities,
            state.original_model_grants,
        )
    ):
        raise FixtureError("original ACL snapshots are required")

    _validate_owned_resource(
        state.skill,
        "skill",
        f"{state.marker}-skill",
        required=require_resources,
    )
    _validate_owned_resource(
        state.knowledge_base,
        "knowledge_base",
        f"{state.marker}-knowledge",
        required=require_resources,
    )
    _validate_owned_resource(
        state.agent,
        "agent",
        f"{state.marker}-agent",
        required=require_resources,
    )
    _validate_owned_resource(
        state.workflow,
        "workflow",
        f"{state.marker}-workflow",
        required=require_resources,
    )
    for value in state.conversation_ids:
        _valid_uuid(value, "conversation_id", required=True)
    for value in state.workflow_run_ids:
        _valid_uuid(value, "workflow_run_id", required=True)
    if len(state.conversation_ids) != len(set(state.conversation_ids)):
        raise FixtureError("conversation_ids must be unique")
    if len(state.workflow_run_ids) != len(set(state.workflow_run_ids)):
        raise FixtureError("workflow_run_ids must be unique")
    if not isinstance(state.prepared, bool):
        raise FixtureError("invalid prepared flag")


def save_state(path: Path, state: FixtureState) -> None:
    validate_owned_state(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                asdict(state),
                stream,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
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


def _load_owned_resource(raw: object, label: str) -> OwnedResource:
    if not isinstance(raw, dict):
        raise FixtureError(f"invalid {label} state")
    allowed = {item.name for item in fields(OwnedResource)}
    if set(raw) - allowed:
        raise FixtureError(f"unknown {label} field")
    try:
        resource = OwnedResource(**raw)
    except TypeError as exc:
        raise FixtureError(f"invalid {label} state") from exc
    if not isinstance(resource.id, str) or not isinstance(resource.name, str):
        raise FixtureError(f"invalid {label} state")
    if not isinstance(resource.initial_grants, list):
        raise FixtureError(f"invalid {label} state")
    return resource


def load_state(path: Path) -> FixtureState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError("could not read fixture state") from exc
    if not isinstance(raw, dict):
        raise FixtureError("fixture state must be a JSON object")
    allowed = {item.name for item in fields(FixtureState)}
    if set(raw) - allowed:
        raise FixtureError("unknown state field")
    for label in ("skill", "knowledge_base", "agent", "workflow"):
        if label in raw:
            raw[label] = _load_owned_resource(raw[label], label)
    try:
        state = FixtureState(**raw)
    except TypeError as exc:
        raise FixtureError("invalid fixture state schema") from exc

    string_fields = (
        "marker",
        "model_id",
        "organization_id",
        "organization_label",
        "analysts_group_id",
        "admin_id",
        "user_id",
        "user2_id",
    )
    if any(not isinstance(getattr(state, name), str) for name in string_fields):
        raise FixtureError("invalid fixture state value types")
    for name in (
        "original_organization_capabilities",
        "original_analysts_capabilities",
        "conversation_ids",
        "workflow_run_ids",
    ):
        value = getattr(state, name)
        if value is not None and (
            not isinstance(value, list)
            or not all(isinstance(item, str) for item in value)
        ):
            raise FixtureError(f"invalid {name}")
    if state.original_model_grants is not None and not isinstance(
        state.original_model_grants, list
    ):
        raise FixtureError("invalid original_model_grants")
    validate_owned_state(state)
    return state


def primary_exit_code(primary: int, cleanup: int) -> int:
    return primary if primary else cleanup


class ApiClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    def token(self, username: str, password: str, client_id: str) -> str:
        encoded = urllib.parse.urlencode(
            {
                "grant_type": "password",
                "client_id": client_id,
                "username": username,
                "password": password,
            }
        ).encode()
        request = urllib.request.Request(
            f"{self.config.keycloak_url}/realms/{self.config.realm}/protocol/openid-connect/token",
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
        except (OSError, json.JSONDecodeError) as exc:
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
        base_url = (
            self.config.business_url if plane == "business" else self.config.admin_url
        )
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
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
        if status == 404:
            return {"_httpStatus": 404}
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
        command = [
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
        ]
        result = subprocess.run(
            command,
            input=sql,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise FixtureError("database operation failed")
        return result.stdout.strip()

    def query_values(self, sql: str) -> list[str]:
        raw = self._run(sql)
        return raw.splitlines() if raw else []

    def execute(self, sql: str) -> None:
        self._run(sql)


class DockerRunnerCleaner:
    @staticmethod
    def _inspect(name: str) -> subprocess.CompletedProcess[str] | None:
        inspected = subprocess.run(
            ["docker", "inspect", name],
            text=True,
            capture_output=True,
            check=False,
        )
        if inspected.returncode == 0:
            return inspected
        if "no such object:" in inspected.stderr.casefold():
            return None
        raise FixtureError("runner inspection failed")

    def remove_owned(self, conversation_id: str) -> None:
        _valid_uuid(conversation_id, "conversation_id", required=True)
        name = f"runner-{conversation_id}"
        inspected = self._inspect(name)
        if inspected is None:
            return
        try:
            payload = json.loads(inspected.stdout)
            details = payload[0]
            container_id = details["Id"]
            config = details["Config"]
            labels = config["Labels"] or {}
            environment = config["Env"] or []
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise FixtureError("runner ownership mismatch") from exc
        if (
            labels.get("aurum-ai.component") != "conversation-runner"
            or f"CONVERSATION_ID={conversation_id}" not in environment
            or not isinstance(container_id, str)
            or not container_id
        ):
            raise FixtureError("runner ownership mismatch")
        removed = subprocess.run(
            ["docker", "rm", "-f", container_id],
            text=True,
            capture_output=True,
            check=False,
        )
        if removed.returncode != 0:
            raise FixtureError("owned runner removal failed")

    def assert_absent(self, conversation_id: str) -> None:
        _valid_uuid(conversation_id, "conversation_id", required=True)
        if self._inspect(f"runner-{conversation_id}") is not None:
            raise FixtureError("owned runner still exists")


def _response_data(response: Any, label: str) -> Any:
    if not isinstance(response, dict) or "data" not in response:
        raise FixtureError(f"{label}: invalid API response")
    return response["data"]


def _canonical_uuid(value: str, label: str) -> str:
    _valid_uuid(value, label, required=True)
    return str(uuid.UUID(value))


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _grant_specs(
    grants: list[dict[str, str | None]],
) -> list[dict[str, str]]:
    specs: list[dict[str, str]] = []
    for grant in grants:
        principal_type = grant.get("principalType")
        principal_id = grant.get("principalId")
        permission = grant.get("permission")
        if principal_type not in {"organization", "group", "user"}:
            raise FixtureError("saved grant principal type is invalid")
        if permission not in {"read", "write"}:
            raise FixtureError("saved grant permission is invalid")
        if not isinstance(principal_id, str):
            raise FixtureError("saved grant principal is invalid")
        specs.append(
            {
                "principalType": principal_type,
                "principalId": _canonical_uuid(principal_id, "grant principal"),
                "permission": permission,
            }
        )
    return specs


def _capability_snapshot(response: Any, label: str) -> list[str]:
    data = _response_data(response, label)
    if not isinstance(data, dict) or not isinstance(data.get("capabilities"), list):
        raise FixtureError(f"{label}: invalid API response")
    capabilities = data["capabilities"]
    if not all(isinstance(item, str) for item in capabilities):
        raise FixtureError(f"{label}: invalid API response")
    snapshot = sorted(set(capabilities))
    _validate_capabilities(snapshot, label)
    return snapshot


def _grant_snapshot(response: Any, label: str) -> list[dict[str, str | None]]:
    data = _response_data(response, label)
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise FixtureError(f"{label}: invalid API response")
    snapshot = [dict(item) for item in data]
    _validate_grants(snapshot, label)
    return snapshot


def sync_identities(config: Config, api: Any | None = None) -> None:
    client = api or ApiClient(config)
    for username in ("admin", "user", "user2"):
        token = client.token(username, username, "business-ui")
        client.request("business", "GET", "/v1/auth/me", token)


class FixtureController:
    def __init__(
        self,
        config: Config,
        *,
        state_path: Path,
        marker: str,
        api: Any | None = None,
        database: Any | None = None,
        runner_cleaner: Any | None = None,
    ) -> None:
        if not OWNED_MARKER.fullmatch(marker):
            raise FixtureError("invalid ownership marker")
        if config.model_id != f"{marker}-model":
            raise FixtureError("model ID does not match ownership marker")
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
        if state.model_id != self.config.model_id:
            raise FixtureError("state model does not match configured model")
        return state

    def _save(self, state: FixtureState) -> None:
        save_state(self.state_path, state)

    @staticmethod
    def _resolve_subject(
        subjects: list[object], principal_type: str, label: str
    ) -> tuple[str, str]:
        matches = [
            item
            for item in subjects
            if isinstance(item, dict)
            and item.get("principalType") == principal_type
            and (label == "" or item.get("label") == label)
        ]
        if len(matches) != 1:
            raise FixtureError(f"authorization subject {principal_type}:{label} is ambiguous")
        principal_id = matches[0].get("principalId")
        subject_label = matches[0].get("label")
        if not isinstance(principal_id, str) or not isinstance(subject_label, str):
            raise FixtureError("authorization subject is invalid")
        return _canonical_uuid(principal_id, f"{principal_type} subject"), subject_label

    def _membership_query(self, state: FixtureState) -> str:
        org_id = _canonical_uuid(state.organization_id, "organization")
        group_id = _canonical_uuid(state.analysts_group_id, "analysts group")
        user_ids = ",".join(
            f"'{_canonical_uuid(value, 'member')}'::uuid"
            for value in (state.user_id, state.user2_id)
        )
        return (
            "SELECT u.id::text FROM user_groups ug "
            "JOIN users u ON u.id=ug.user_id "
            "JOIN groups g ON g.id=ug.group_id "
            f"WHERE u.org_id='{org_id}'::uuid AND g.org_id='{org_id}'::uuid "
            f"AND g.id='{group_id}'::uuid AND u.id IN ({user_ids}) ORDER BY u.id;"
        )

    @staticmethod
    def _runtime_prompts(state: FixtureState) -> tuple[str, ...]:
        return (
            f"{state.marker}-success",
            f"{state.marker}-live-skill",
            f"{state.marker}-agent-revoked",
            f"{state.marker}-knowledge-revoked",
        )

    def _collision_query(self, state: FixtureState) -> str:
        org_id = _canonical_uuid(state.organization_id, "organization")
        user_id = _canonical_uuid(state.user_id, "user")
        prompts = ",".join(
            _sql_text(value) for value in self._runtime_prompts(state)
        )
        names = {
            "skill": f"{state.marker}-skill",
            "knowledge": f"{state.marker}-knowledge",
            "agent": f"{state.marker}-agent",
            "workflow": f"{state.marker}-workflow",
        }
        return (
            "SELECT 'skill:' || id::text FROM skills "
            f"WHERE org_id='{org_id}'::uuid AND name={_sql_text(names['skill'])} UNION ALL "
            "SELECT 'knowledge:' || id::text FROM knowledge_bases "
            f"WHERE org_id='{org_id}'::uuid AND name={_sql_text(names['knowledge'])} UNION ALL "
            "SELECT 'agent:' || id::text FROM agent_definitions "
            f"WHERE org_id='{org_id}'::uuid AND name={_sql_text(names['agent'])} UNION ALL "
            "SELECT 'workflow:' || id::text FROM workflow_definitions "
            f"WHERE org_id='{org_id}'::uuid AND name={_sql_text(names['workflow'])} UNION ALL "
            "SELECT 'conversation:' || id::text FROM conversations "
            f"WHERE org_id='{org_id}'::uuid AND user_id='{user_id}'::uuid "
            f"AND title IN ({prompts});"
        )

    def _read_grants(
        self,
        token: str,
        resource_type: str,
        resource_id: str,
    ) -> list[dict[str, str | None]]:
        encoded_type = urllib.parse.quote(resource_type, safe="")
        encoded_id = urllib.parse.quote(resource_id, safe="")
        return _grant_snapshot(
            self.api.request(
                "admin",
                "GET",
                "/v1/admin/authorization/grants"
                f"?resourceType={encoded_type}&resourceId={encoded_id}",
                token,
            ),
            f"{resource_type} grants",
        )

    @staticmethod
    def _created_id(response: Any, label: str) -> str:
        data = _response_data(response, label)
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise FixtureError(f"{label}: invalid API response")
        return _canonical_uuid(data["id"], label)

    def prepare(self) -> None:
        if self.state_path.exists():
            raise FixtureError("authorization UI recovery state already exists")
        state = FixtureState(marker=self.marker, model_id=self.config.model_id)
        self._save(state)

        admin_business = self.api.token("admin", "admin", "business-ui")
        admin_admin = self.api.token("admin", "admin", "admin-ui")
        user_business = self.api.token("user", "user", "business-ui")
        user2_business = self.api.token("user2", "user2", "business-ui")
        for token in (admin_business, user_business, user2_business):
            self.api.request("business", "GET", "/v1/auth/me", token)

        subjects_raw = _response_data(
            self.api.request(
                "admin",
                "GET",
                "/v1/admin/authorization/subjects",
                admin_admin,
            ),
            "authorization subjects",
        )
        if not isinstance(subjects_raw, list):
            raise FixtureError("authorization subjects: invalid API response")
        state.organization_id, state.organization_label = self._resolve_subject(
            subjects_raw, "organization", ""
        )
        state.analysts_group_id, _ = self._resolve_subject(
            subjects_raw, "group", "analysts"
        )
        state.admin_id, _ = self._resolve_subject(subjects_raw, "user", "Admin User")
        state.user_id, _ = self._resolve_subject(subjects_raw, "user", "Regular User")
        state.user2_id, _ = self._resolve_subject(subjects_raw, "user", "Second User")
        self._save(state)

        members = self.database.query_values(self._membership_query(state))
        if members != [state.user_id]:
            raise FixtureError("unexpected analysts group membership")
        if self.database.query_values(self._collision_query(state)):
            raise FixtureError("authorization UI ownership collision")

        state.original_organization_capabilities = _capability_snapshot(
            self.api.request(
                "admin",
                "GET",
                "/v1/admin/authorization/capabilities/organization",
                admin_admin,
            ),
            "organization capabilities",
        )
        state.original_analysts_capabilities = _capability_snapshot(
            self.api.request(
                "admin",
                "GET",
                "/v1/admin/authorization/capabilities/groups/"
                f"{state.analysts_group_id}",
                admin_admin,
            ),
            "analysts capabilities",
        )
        state.original_model_grants = self._read_grants(
            admin_admin, "model", state.model_id
        )
        self._save(state)
        if "workspace.knowledge.create" in (
            state.original_organization_capabilities
        ) or "workspace.knowledge.create" in state.original_analysts_capabilities:
            raise FixtureError("workspace.knowledge.create already assigned")
        if state.original_model_grants:
            raise FixtureError("marker model grants must be empty")

        names = {
            "skill": f"{state.marker}-skill",
            "knowledge_base": f"{state.marker}-knowledge",
            "agent": f"{state.marker}-agent",
            "workflow": f"{state.marker}-workflow",
        }
        state.skill.name = names["skill"]
        state.skill.id = self._created_id(
            self.api.request(
                "admin",
                "POST",
                "/v1/skills/",
                admin_admin,
                body={
                    "name": names["skill"],
                    "description": "Authorization UI dependency",
                    "type": "tool",
                },
                expected=(201,),
            ),
            "create skill",
        )
        self._save(state)
        state.skill.initial_grants = self._read_grants(
            admin_admin, "skill", state.skill.id
        )
        self._save(state)

        state.knowledge_base.name = names["knowledge_base"]
        state.knowledge_base.id = self._created_id(
            self.api.request(
                "admin",
                "POST",
                "/v1/knowledge-bases",
                admin_admin,
                body={
                    "name": names["knowledge_base"],
                    "description": "Authorization UI dependency",
                },
                expected=(201,),
            ),
            "create knowledge base",
        )
        self._save(state)
        state.knowledge_base.initial_grants = self._read_grants(
            admin_admin, "knowledge_base", state.knowledge_base.id
        )
        self._save(state)

        state.agent.name = names["agent"]
        state.agent.id = self._created_id(
            self.api.request(
                "admin",
                "POST",
                "/v1/agent-definitions",
                admin_admin,
                body={
                    "name": names["agent"],
                    "description": "Authorization UI dependency",
                    "modelId": state.model_id,
                    "systemPrompt": "Deterministic Authorization UI workflow fixture.",
                    "temperature": 0,
                    "skillIds": [state.skill.id],
                    "knowledgeBaseIds": [state.knowledge_base.id],
                },
                expected=(201,),
            ),
            "create agent definition",
        )
        self._save(state)
        state.agent.initial_grants = self._read_grants(
            admin_admin, "agent_definition", state.agent.id
        )
        self._save(state)

        state.workflow.name = names["workflow"]
        workflow_response = self.api.request(
            "admin",
            "POST",
            "/v1/admin/workflows",
            admin_admin,
            body={
                "name": names["workflow"],
                "description": "Authorization UI dependency",
                "supervisorAgentDefinitionId": state.agent.id,
                "maxParallelism": 1,
                "nodes": [
                    {
                        "nodeKey": "authorized-node",
                        "displayName": "Authorization node",
                        "agentDefinitionId": state.agent.id,
                        "instruction": (
                            "MULTI_AGENT_NODE:authorized-node "
                            "MULTI_AGENT_BARRIER:authorization-ui"
                        ),
                        "positionX": 0,
                        "positionY": 0,
                    }
                ],
                "edges": [],
            },
            expected=(201,),
        )
        state.workflow.id = self._created_id(workflow_response, "create workflow")
        workflow_data = _response_data(workflow_response, "create workflow")
        if not isinstance(workflow_data, dict) or workflow_data.get("status") != "active":
            raise FixtureError("created workflow is not active")
        self._save(state)
        state.workflow.initial_grants = self._read_grants(
            admin_admin, "workflow", state.workflow.id
        )
        state.prepared = True
        self._save(state)

    def _discover_runtime_query(self, state: FixtureState) -> str:
        org_id = _canonical_uuid(state.organization_id, "organization")
        user_id = _canonical_uuid(state.user_id, "user")
        workflow_id = _canonical_uuid(state.workflow.id, "workflow")
        prompts = ",".join(
            _sql_text(value) for value in self._runtime_prompts(state)
        )
        return (
            "SELECT wr.conversation_id::text || '|' || wr.id::text "
            "FROM workflow_runs wr JOIN messages m "
            "ON m.org_id=wr.org_id AND m.id=wr.source_message_id "
            f"WHERE wr.org_id='{org_id}'::uuid "
            f"AND wr.initiating_user_id='{user_id}'::uuid "
            f"AND wr.workflow_definition_id='{workflow_id}'::uuid "
            f"AND m.role='user' AND m.content IN ({prompts}) "
            "UNION SELECT c.id::text || '|' FROM conversations c "
            f"WHERE c.org_id='{org_id}'::uuid "
            f"AND c.user_id='{user_id}'::uuid AND c.title IN ({prompts}) "
            "ORDER BY 1;"
        )

    def _discover_runtime(self, state: FixtureState) -> None:
        if not state.workflow.id:
            return
        rows = self.database.query_values(self._discover_runtime_query(state))
        conversations = set(state.conversation_ids)
        runs = set(state.workflow_run_ids)
        for row in rows:
            parts = row.split("|")
            if len(parts) != 2:
                raise FixtureError("runtime discovery returned invalid ownership row")
            conversations.add(_canonical_uuid(parts[0], "conversation"))
            if parts[1]:
                runs.add(_canonical_uuid(parts[1], "workflow run"))
        state.conversation_ids = sorted(conversations)
        state.workflow_run_ids = sorted(runs)
        self._save(state)

    def _cleanup_sql(self, state: FixtureState) -> str:
        org_id = _canonical_uuid(state.organization_id, "organization")
        user_id = _canonical_uuid(state.user_id, "user")
        workflow_id = _canonical_uuid(state.workflow.id, "workflow")
        workflow_name = _sql_text(state.workflow.name)
        run_ids = ",".join(
            f"'{_canonical_uuid(value, 'workflow run')}'::uuid"
            for value in state.workflow_run_ids
        )
        conversation_ids = ",".join(
            f"'{_canonical_uuid(value, 'conversation')}'::uuid"
            for value in state.conversation_ids
        )
        statements = [
            "DELETE FROM access_grants "
            f"WHERE org_id='{org_id}'::uuid AND resource_type='workflow' "
            f"AND resource_id='{workflow_id}'",
        ]
        if run_ids:
            statements.append(
                "UPDATE llm_usage_events SET workflow_run_id=NULL, "
                "workflow_phase=NULL, workflow_node_key=NULL "
                f"WHERE org_id='{org_id}'::uuid AND workflow_run_id IN ({run_ids})"
            )
        if conversation_ids:
            statements.append(
                f"DELETE FROM conversations WHERE org_id='{org_id}'::uuid "
                f"AND user_id='{user_id}'::uuid AND id IN ({conversation_ids})"
            )
        statements.append(
            f"DELETE FROM workflow_definitions WHERE org_id='{org_id}'::uuid "
            f"AND id='{workflow_id}'::uuid AND name={workflow_name}"
        )
        return "BEGIN;\n" + ";\n".join(statements) + ";\nCOMMIT;"

    def _restore_configuration(self, state: FixtureState, token: str) -> list[str]:
        errors: list[str] = []
        restorations: list[tuple[str, str, dict[str, object]]] = []
        if state.original_model_grants is not None:
            try:
                response = self.api.request(
                    "admin",
                    "PUT",
                    "/v1/admin/authorization/grants/reconcile",
                    token,
                    body={
                        "resourceType": "model",
                        "resourceId": state.model_id,
                        "grants": _grant_specs(state.original_model_grants),
                    },
                    expected=(200, 404),
                )
                if (
                    isinstance(response, dict)
                    and response.get("_httpStatus") == 404
                    and state.original_model_grants
                ):
                    errors.append("restore model grants: virtual model is missing")
            except FixtureError as exc:
                errors.append(f"restore model grants: {exc}")
        if state.original_analysts_capabilities is not None:
            restorations.append(
                (
                    "analysts capabilities",
                    "/v1/admin/authorization/capabilities/groups/"
                    f"{state.analysts_group_id}",
                    {"capabilities": state.original_analysts_capabilities},
                )
            )
        if state.original_organization_capabilities is not None:
            restorations.append(
                (
                    "organization capabilities",
                    "/v1/admin/authorization/capabilities/organization",
                    {"capabilities": state.original_organization_capabilities},
                )
            )
        for label, path, body in restorations:
            try:
                self.api.request("admin", "PUT", path, token, body=body)
            except FixtureError as exc:
                errors.append(f"restore {label}: {exc}")
        return errors

    def _delete_owned_api_resource(
        self,
        token: str,
        resource: OwnedResource,
        path: str,
        label: str,
    ) -> None:
        if not resource.id:
            return
        response = self.api.request(
            "admin", "GET", f"{path}/{resource.id}", token, expected=(200, 404)
        )
        if isinstance(response, dict) and response.get("_httpStatus") == 404:
            return
        data = _response_data(response, f"read owned {label}")
        if not isinstance(data, dict) or data.get("name") != resource.name:
            raise FixtureError(f"{label} ownership mismatch")
        self.api.request(
            "admin",
            "DELETE",
            f"{path}/{resource.id}",
            token,
            expected=(204, 404),
        )

    def cleanup(self) -> None:
        if not self.state_path.exists():
            return
        state = self._state()
        validate_owned_state(state)
        errors: list[str] = []
        try:
            admin_token = self.api.token("admin", "admin", "admin-ui")
        except FixtureError as exc:
            raise FixtureError(f"cleanup authentication failed: {exc}") from exc

        errors.extend(self._restore_configuration(state, admin_token))

        if state.organization_id and state.workflow.id:
            try:
                self._discover_runtime(state)
            except FixtureError as exc:
                errors.append(f"runtime discovery: {exc}")
            for conversation_id in state.conversation_ids:
                try:
                    self.runner_cleaner.remove_owned(conversation_id)
                except FixtureError as exc:
                    errors.append(f"runner cleanup: {exc}")
            try:
                self.database.execute(self._cleanup_sql(state))
            except FixtureError as exc:
                errors.append(f"owned runtime/workflow cleanup: {exc}")

        for resource, path, label in (
            (state.agent, "/v1/agent-definitions", "agent"),
            (state.knowledge_base, "/v1/knowledge-bases", "knowledge base"),
            (state.skill, "/v1/skills", "skill"),
        ):
            try:
                self._delete_owned_api_resource(
                    admin_token, resource, path, label
                )
            except FixtureError as exc:
                errors.append(f"delete {label}: {exc}")

        try:
            self.verify_clean()
        except FixtureError as exc:
            errors.append(f"verify clean: {exc}")
        if errors:
            raise FixtureError("; ".join(errors))

    def _verification_query(self, state: FixtureState) -> tuple[str, set[str]]:
        org_id = _canonical_uuid(state.organization_id, "organization")
        checks: list[tuple[str, str]] = []
        row_queries: list[str] = []
        expected_rows: set[str] = set()
        for label, table, resource in (
            ("skill", "skills", state.skill),
            ("knowledge", "knowledge_bases", state.knowledge_base),
            ("agent", "agent_definitions", state.agent),
            ("workflow", "workflow_definitions", state.workflow),
        ):
            if resource.id:
                resource_id = _canonical_uuid(resource.id, label)
                checks.append(
                    (
                        label,
                        f"SELECT count(*) FROM {table} WHERE org_id='{org_id}'::uuid "
                        f"AND id='{resource_id}'::uuid AND name={_sql_text(resource.name)}",
                    )
                )
        checks.append(
            (
                "model-grant",
                "SELECT count(*) FROM access_grants "
                f"WHERE org_id='{org_id}'::uuid AND resource_type='model' "
                f"AND resource_id={_sql_text(state.model_id)}",
            )
        )
        if state.conversation_ids:
            rendered = ",".join(
                f"'{_canonical_uuid(item, 'conversation')}'::uuid"
                for item in state.conversation_ids
            )
            checks.append(
                (
                    "conversation",
                    "SELECT count(*) FROM conversations "
                    f"WHERE org_id='{org_id}'::uuid AND id IN ({rendered})",
                )
            )
        if state.workflow_run_ids:
            rendered = ",".join(
                f"'{_canonical_uuid(item, 'workflow run')}'::uuid"
                for item in state.workflow_run_ids
            )
            checks.append(
                (
                    "workflow-run",
                    "SELECT count(*) FROM workflow_runs "
                    f"WHERE org_id='{org_id}'::uuid AND id IN ({rendered})",
                )
            )
        if state.original_organization_capabilities is not None:
            row_queries.append(
                "SELECT 'organization-capability|' || capability "
                "FROM organization_capabilities "
                f"WHERE org_id='{org_id}'::uuid"
            )
            expected_rows.update(
                f"organization-capability|{capability}"
                for capability in state.original_organization_capabilities
            )
        if state.original_analysts_capabilities is not None:
            group_id = _canonical_uuid(state.analysts_group_id, "analysts group")
            row_queries.append(
                "SELECT 'group-capability|' || capability "
                "FROM group_capabilities "
                f"WHERE group_id='{group_id}'::uuid"
            )
            expected_rows.update(
                f"group-capability|{capability}"
                for capability in state.original_analysts_capabilities
            )
        query_parts = [
            f"SELECT {_sql_text(label + '|')} || ({query})::text"
            for label, query in checks
        ]
        query_parts.extend(row_queries)
        sql = " UNION ALL ".join(query_parts)
        if sql:
            sql += ";"
        expected_rows.update(f"{label}|0" for label, _query in checks)
        return sql, expected_rows

    def verify_clean(self) -> None:
        if not self.state_path.exists():
            return
        state = self._state()
        if not state.organization_id:
            return
        sql, expected = self._verification_query(state)
        if sql and set(self.database.query_values(sql)) != expected:
            raise FixtureError("owned resources or ACL state remain")
        for conversation_id in state.conversation_ids:
            self.runner_cleaner.assert_absent(conversation_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync-identities")
    for command in ("prepare", "cleanup", "verify-clean"):
        child = commands.add_parser(command)
        child.add_argument("--state", type=Path, required=True)
        child.add_argument("--marker", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = Config()
    try:
        if args.command == "sync-identities":
            sync_identities(config)
            return 0
        # Recovery runs outside the wrapper's exported environment. The marker
        # derives the owned model; controller/state validation still rejects
        # invalid markers, mismatched journals and explicit foreign model IDs.
        if args.command in {"cleanup", "verify-clean"} and not config.model_id:
            config = replace(config, model_id=f"{args.marker}-model")
        controller = FixtureController(
            config, state_path=args.state, marker=args.marker
        )
        if args.command == "prepare":
            controller.prepare()
        elif args.command == "cleanup":
            controller.cleanup()
        else:
            controller.verify_clean()
    except FixtureError as exc:
        print(f"[authorization-ui-e2e] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
