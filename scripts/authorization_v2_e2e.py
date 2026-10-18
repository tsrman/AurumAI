#!/usr/bin/env python3
"""Deterministic Authorization v2 acceptance for Compose and Kubernetes.

The harness owns every resource it creates, snapshots capability assignments
before changing them, and restores Keycloak membership on every exit path. It
never prints bearer/session tokens or request payloads.
"""

from __future__ import annotations

from collections.abc import Callable
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any


class SmokeFailure(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[authorization-v2-smoke] {message}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Config:
    business_url: str = field(
        default_factory=lambda: os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    )
    admin_url: str = field(
        default_factory=lambda: os.getenv("ADMIN_BASE_URL", "http://localhost:8001").rstrip("/")
    )
    keycloak_url: str = field(
        default_factory=lambda: os.getenv("KEYCLOAK_URL", "http://localhost:8080").rstrip("/")
    )
    realm: str = field(default_factory=lambda: os.getenv("KEYCLOAK_REALM", "aurum-ai"))
    business_client: str = field(
        default_factory=lambda: os.getenv("KEYCLOAK_CLIENT_ID", "business-ui")
    )
    admin_client: str = field(
        default_factory=lambda: os.getenv("ADMIN_KEYCLOAK_CLIENT_ID", "admin-ui")
    )
    model: str = field(default_factory=lambda: os.getenv("GROUPS_SMOKE_MODEL", "gpt-4o-mini"))
    runtime: str = field(
        default_factory=lambda: os.getenv("AUTHORIZATION_E2E_RUNTIME", "docker")
    )
    db_container: str = field(
        default_factory=lambda: os.getenv("AUTHORIZATION_E2E_DB_CONTAINER", "aurum-ai-postgres")
    )
    kubeconfig: str | None = field(
        default_factory=lambda: os.getenv("AUTHORIZATION_E2E_KUBECONFIG")
    )
    runner_namespace: str = field(
        default_factory=lambda: os.getenv("AUTHORIZATION_E2E_RUNNER_NAMESPACE", "aurum-ai-runners")
    )
    session_secret: str = field(
        default_factory=lambda: os.getenv("AUTHORIZATION_E2E_SESSION_SECRET")
        or os.getenv("APP_SECRET_KEY", "")
    )


class HttpClient:
    def request(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
        timeout: float = 30,
    ) -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        data: bytes | None = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode()
        elif form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = urllib.parse.urlencode(form).encode()
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                body = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read()
        except OSError as exc:
            raise SmokeFailure(f"{method} {url}: transport failed: {exc}") from exc
        parsed: Any = None
        if body:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = body.decode(errors="replace")
        if status not in expected:
            raise SmokeFailure(
                f"{method} {url}: expected HTTP {expected}, got {status}: {parsed!r}"
            )
        return status, parsed

    def wait(self, url: str, name: str, timeout: int = 120) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.request("GET", url, expected=(200,), timeout=2)
                log(f"{name} is ready")
                return
            except SmokeFailure:
                time.sleep(1)
        raise SmokeFailure(f"{name} did not become ready within {timeout}s")


@dataclass
class State:
    organization_id: str | None = None
    analysts_id: str | None = None
    engineers_id: str | None = None
    user_id: str | None = None
    user2_id: str | None = None
    skill_id: str | None = None
    knowledge_base_id: str | None = None
    agent_id: str | None = None
    conversation_id: str | None = None
    runner_identity: str | None = None
    org_capabilities: list[str] | None = None
    analysts_capabilities: list[str] | None = None
    engineers_capabilities: list[str] | None = None
    model_grants: list[dict[str, str]] | None = None
    keycloak_user_id: str | None = None
    keycloak_engineers_id: str | None = None
    engineers_membership_added: bool = False
    foreign_org_id: str | None = None
    foreign_skill_id: str | None = None
    admin_business_token: str | None = None
    admin_admin_token: str | None = None
    user_business_token: str | None = None
    user_admin_token: str | None = None
    user2_business_token: str | None = None
    master_token: str | None = None


class Acceptance:
    def __init__(self, config: Config, http: HttpClient | None = None) -> None:
        self.config = config
        self.http = http or HttpClient()
        self.state = State()
        self.marker = f"authorization-v2-e2e-{uuid.uuid4().hex[:10]}"

    def token(self, username: str, password: str, client_id: str) -> str:
        _, body = self.http.request(
            "POST",
            f"{self.config.keycloak_url}/realms/{self.config.realm}/protocol/openid-connect/token",
            form={
                "grant_type": "password",
                "client_id": client_id,
                "username": username,
                "password": password,
            },
        )
        return str(body["access_token"])

    def master_token(self) -> str:
        username = os.getenv("KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME", "admin")
        # Bootstrap stores the generated value as KEYCLOAK_ADMIN_PASSWORD; "admin"
        # only matches databases initialized before that value existed.
        password = (
            os.getenv("KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD")
            or os.getenv("KEYCLOAK_ADMIN_PASSWORD")
            or "admin"
        )
        _, body = self.http.request(
            "POST",
            f"{self.config.keycloak_url}/realms/master/protocol/openid-connect/token",
            form={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": username,
                "password": password,
            },
        )
        return str(body["access_token"])

    def api(
        self,
        plane: str,
        method: str,
        path: str,
        token: str | None,
        *,
        payload: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
        timeout: float = 30,
    ) -> tuple[int, Any]:
        base = self.config.business_url if plane == "business" else self.config.admin_url
        return self.http.request(
            method,
            f"{base}{path}",
            token=token,
            payload=payload,
            expected=expected,
            timeout=timeout,
        )

    def keycloak_admin(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...] = (200,),
    ) -> tuple[int, Any]:
        assert self.state.master_token
        return self.http.request(
            method,
            f"{self.config.keycloak_url}/admin/realms/{self.config.realm}{path}",
            token=self.state.master_token,
            expected=expected,
        )

    def acquire_tokens(self) -> None:
        self.state.admin_business_token = self.token(
            "admin", "admin", self.config.business_client
        )
        self.state.admin_admin_token = self.token("admin", "admin", self.config.admin_client)
        self.state.user_business_token = self.token("user", "user", self.config.business_client)
        self.state.user_admin_token = self.token("user", "user", self.config.admin_client)
        self.state.user2_business_token = self.token(
            "user2", "user2", self.config.business_client
        )
        self.state.master_token = self.master_token()

    def sync_users(self) -> None:
        for token in (
            self.state.admin_business_token,
            self.state.user_business_token,
            self.state.user2_business_token,
        ):
            self.api("business", "GET", "/v1/auth/me", token)

    def add_second_group_membership(self) -> None:
        _, users = self.keycloak_admin("GET", "/users?username=user&exact=true")
        _, groups = self.keycloak_admin("GET", "/groups?search=engineers&exact=true")
        user = next(item for item in users if item["username"] == "user")
        engineers = next(item for item in groups if item["name"] == "engineers")
        self.state.keycloak_user_id = str(user["id"])
        self.state.keycloak_engineers_id = str(engineers["id"])
        _, memberships = self.keycloak_admin(
            "GET", f"/users/{self.state.keycloak_user_id}/groups"
        )
        if any(str(item["id"]) == self.state.keycloak_engineers_id for item in memberships):
            return
        self.keycloak_admin(
            "PUT",
            f"/users/{self.state.keycloak_user_id}/groups/{self.state.keycloak_engineers_id}",
            expected=(204,),
        )
        self.state.engineers_membership_added = True
        self.state.user_business_token = self.token("user", "user", self.config.business_client)
        self.state.user_admin_token = self.token("user", "user", self.config.admin_client)
        self.api("business", "GET", "/v1/auth/me", self.state.user_business_token)

    def load_subjects(self) -> None:
        _, body = self.api(
            "admin",
            "GET",
            "/v1/admin/authorization/subjects",
            self.state.admin_admin_token,
        )
        subjects = body["data"]

        def subject_id(principal_type: str, label: str) -> str:
            matches = [
                item
                for item in subjects
                if item["principalType"] == principal_type and item["label"] == label
            ]
            if len(matches) != 1:
                raise SmokeFailure(
                    f"expected one {principal_type} subject labelled {label!r}, got {len(matches)}"
                )
            return str(matches[0]["principalId"])

        organizations = [item for item in subjects if item["principalType"] == "organization"]
        if len(organizations) != 1:
            raise SmokeFailure(f"expected one organization subject, got {len(organizations)}")
        self.state.organization_id = str(organizations[0]["principalId"])
        self.state.analysts_id = subject_id("group", "analysts")
        self.state.engineers_id = subject_id("group", "engineers")
        self.state.user_id = subject_id("user", "Regular User")
        self.state.user2_id = subject_id("user", "Second User")

        self.api(
            "admin",
            "GET",
            "/v1/admin/authorization/subjects",
            self.state.user_admin_token,
            expected=(403,),
        )

    def capability_path(self, group_id: str | None = None) -> str:
        if group_id is None:
            return "/v1/admin/authorization/capabilities/organization"
        return f"/v1/admin/authorization/capabilities/groups/{group_id}"

    def get_capabilities(self, group_id: str | None = None) -> list[str]:
        _, body = self.api(
            "admin",
            "GET",
            self.capability_path(group_id),
            self.state.admin_admin_token,
        )
        return [str(item) for item in body["data"]["capabilities"]]

    def put_capabilities(self, capabilities: list[str], group_id: str | None = None) -> None:
        self.api(
            "admin",
            "PUT",
            self.capability_path(group_id),
            self.state.admin_admin_token,
            payload={"capabilities": capabilities},
        )

    def configure_capabilities(self) -> None:
        assert self.state.analysts_id and self.state.engineers_id
        self.state.org_capabilities = self.get_capabilities()
        self.state.analysts_capabilities = self.get_capabilities(self.state.analysts_id)
        self.state.engineers_capabilities = self.get_capabilities(self.state.engineers_id)
        self.put_capabilities(
            sorted(set(self.state.org_capabilities) | {"chat.send"})
        )
        self.put_capabilities(
            sorted(
                set(self.state.analysts_capabilities)
                | {"workspace.knowledge.create"}
            ),
            self.state.analysts_id,
        )

    def create_resources(self) -> None:
        _, skill = self.api(
            "admin",
            "POST",
            "/v1/skills/",
            self.state.admin_admin_token,
            payload={"name": f"{self.marker}-skill", "type": "tool"},
            expected=(201,),
        )
        self.state.skill_id = str(skill["data"]["id"])
        _, knowledge = self.api(
            "admin",
            "POST",
            "/v1/knowledge-bases",
            self.state.admin_admin_token,
            payload={"name": f"{self.marker}-knowledge"},
            expected=(201,),
        )
        self.state.knowledge_base_id = str(knowledge["data"]["id"])
        _, agent = self.api(
            "admin",
            "POST",
            "/v1/agent-definitions",
            self.state.admin_admin_token,
            payload={
                "name": f"{self.marker}-agent",
                "modelId": self.config.model,
                "systemPrompt": "Authorization v2 deterministic acceptance agent",
                "skillIds": [self.state.skill_id],
                "knowledgeBaseIds": [self.state.knowledge_base_id],
            },
            expected=(201,),
        )
        self.state.agent_id = str(agent["data"]["id"])

    def reconcile(
        self,
        resource_type: str,
        resource_id: str,
        grants: list[dict[str, str]],
    ) -> None:
        self.api(
            "admin",
            "PUT",
            "/v1/admin/authorization/grants/reconcile",
            self.state.admin_admin_token,
            payload={
                "resourceType": resource_type,
                "resourceId": resource_id,
                "grants": grants,
            },
        )

    def configure_grants(self) -> None:
        assert all(
            (
                self.state.organization_id,
                self.state.analysts_id,
                self.state.engineers_id,
                self.state.user_id,
                self.state.skill_id,
                self.state.knowledge_base_id,
                self.state.agent_id,
            )
        )
        _, current_model_grants = self.api(
            "admin",
            "GET",
            "/v1/admin/authorization/grants"
            f"?resourceType=model&resourceId={urllib.parse.quote(self.config.model)}",
            self.state.admin_admin_token,
        )
        self.state.model_grants = [
            {
                "principalType": str(item["principalType"]),
                "principalId": str(item["principalId"]),
                "permission": str(item["permission"]),
            }
            for item in current_model_grants["data"]
        ]
        self.reconcile(
            "model",
            self.config.model,
            [
                {
                    "principalType": "organization",
                    "principalId": self.state.organization_id or "",
                    "permission": "read",
                }
            ],
        )
        self.reconcile(
            "skill",
            self.state.skill_id or "",
            [
                {
                    "principalType": "group",
                    "principalId": self.state.engineers_id or "",
                    "permission": "read",
                }
            ],
        )
        self.reconcile(
            "knowledge_base",
            self.state.knowledge_base_id or "",
            [
                {
                    "principalType": "group",
                    "principalId": self.state.analysts_id or "",
                    "permission": "write",
                }
            ],
        )
        self.reconcile(
            "agent_definition",
            self.state.agent_id or "",
            [
                {
                    "principalType": "user",
                    "principalId": self.state.user_id or "",
                    "permission": "read",
                }
            ],
        )

    def seed_foreign_resource(self) -> None:
        self.state.foreign_org_id = str(uuid.uuid4())
        foreign_user_id = str(uuid.uuid4())
        self.state.foreign_skill_id = str(uuid.uuid4())
        sql = f"""
INSERT INTO organizations (id, name, slug)
VALUES ('{self.state.foreign_org_id}', 'Authorization v2 foreign fixture', '{self.marker}-foreign');
INSERT INTO users (id, keycloak_id, email, org_id, role, status)
VALUES ('{foreign_user_id}', '{foreign_user_id}', '{self.marker}@foreign.invalid',
        '{self.state.foreign_org_id}', 'member', 'active');
INSERT INTO skills (id, org_id, name, type, input_schema, network_policy)
VALUES ('{self.state.foreign_skill_id}', '{self.state.foreign_org_id}',
        '{self.marker}-foreign-skill', 'tool', '{{}}'::json, 'none');
"""
        self.db_sql(sql)

    def db_sql(self, sql: str) -> None:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                self.config.db_container,
                "psql",
                "-v",
                "ON_ERROR_STOP=1",
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
            raise SmokeFailure(f"foreign fixture SQL failed: {result.stderr.strip()}")

    def verify_access_matrix(self) -> None:
        assert self.state.skill_id and self.state.knowledge_base_id and self.state.agent_id
        user = self.state.user_business_token
        user_admin = self.state.user_admin_token
        user2 = self.state.user2_business_token
        admin = self.state.admin_business_token

        self.api("business", "GET", f"/v1/skills/{self.state.skill_id}", user)
        self.api(
            "admin",
            "PATCH",
            f"/v1/skills/{self.state.skill_id}",
            user_admin,
            payload={"description": "read must not imply write"},
            expected=(403,),
        )
        _, knowledge = self.api(
            "business",
            "GET",
            f"/v1/knowledge-bases/{self.state.knowledge_base_id}",
            user,
        )
        if knowledge["data"]["canWrite"] is not True:
            raise SmokeFailure("knowledge-base write grant did not imply read access")
        self.api("business", "GET", f"/v1/agent-definitions/{self.state.agent_id}", user)
        self.api(
            "business",
            "GET",
            f"/v1/agent-definitions/{self.state.agent_id}/execution",
            user,
        )

        _, models = self.api("business", "GET", "/v1/models", user2)
        if self.config.model not in {item["id"] for item in models["data"]}:
            raise SmokeFailure("organization model grant was not visible to user2")
        for path in (
            f"/v1/skills/{self.state.skill_id}",
            f"/v1/knowledge-bases/{self.state.knowledge_base_id}",
            f"/v1/agent-definitions/{self.state.agent_id}",
        ):
            self.api("business", "GET", path, user2, expected=(404,))

        for path in (
            f"/v1/skills/{self.state.skill_id}",
            f"/v1/knowledge-bases/{self.state.knowledge_base_id}",
            f"/v1/agent-definitions/{self.state.agent_id}",
        ):
            self.api("business", "GET", path, admin)
        self.api(
            "business",
            "GET",
            f"/v1/skills/{self.state.skill_id}",
            None,
            expected=(401,),
        )
        self.api(
            "admin",
            "POST",
            "/v1/admin/authorization/grants",
            self.state.admin_admin_token,
            payload={
                "resourceType": "skill",
                "resourceId": self.state.skill_id,
                "principalType": "anonymous",
                "principalId": str(uuid.uuid4()),
                "permission": "read",
            },
            expected=(400,),
        )
        if self.state.foreign_skill_id:
            self.api(
                "business",
                "GET",
                f"/v1/skills/{self.state.foreign_skill_id}",
                user,
                expected=(404,),
            )

    def verify_preview(self) -> None:
        assert self.state.user_id
        resources = [
            {"resourceType": "model", "resourceId": self.config.model},
            {"resourceType": "skill", "resourceId": self.state.skill_id},
            {
                "resourceType": "knowledge_base",
                "resourceId": self.state.knowledge_base_id,
            },
            {"resourceType": "agent_definition", "resourceId": self.state.agent_id},
        ]
        _, body = self.api(
            "admin",
            "POST",
            f"/v1/admin/authorization/preview/users/{self.state.user_id}",
            self.state.admin_admin_token,
            payload={"resources": resources},
        )
        preview = body["data"]
        expected_capabilities = {"chat.send", "workspace.knowledge.create"}
        if not expected_capabilities.issubset(set(preview["capabilities"])):
            raise SmokeFailure(
                f"effective capabilities missing from preview: {preview['capabilities']!r}"
            )
        decisions = {
            (item["resourceType"], item["resourceId"]): item
            for item in preview["resources"]
        }
        expected = {
            ("model", self.config.model): (True, False),
            ("skill", self.state.skill_id): (True, False),
            ("knowledge_base", self.state.knowledge_base_id): (True, True),
            ("agent_definition", self.state.agent_id): (True, False),
        }
        for key, (can_read, can_write) in expected.items():
            decision = decisions[key]
            if (decision["canRead"], decision["canWrite"], decision["source"]) != (
                can_read,
                can_write,
                "grant",
            ):
                raise SmokeFailure(f"preview decision differs for {key}: {decision!r}")

    def create_active_runner(self) -> tuple[str, str]:
        _, body = self.api(
            "business",
            "POST",
            "/v1/conversations",
            self.state.user_business_token,
            payload={"title": self.marker, "model": self.config.model},
            expected=(200, 201),
        )
        self.state.conversation_id = str(body["data"]["id"])
        self.api(
            "business",
            "POST",
            "/v1/chat/stream",
            self.state.user_business_token,
            payload={
                "conversation_id": self.state.conversation_id,
                "messages": [{"role": "user", "content": "authorization v2 acceptance"}],
                "model": self.config.model,
            },
            timeout=120,
        )
        return self.runner_identity_and_token()

    def runner_identity_and_token(self) -> tuple[str, str]:
        assert self.state.conversation_id
        if not self.config.session_secret:
            raise SmokeFailure(
                "AUTHORIZATION_E2E_SESSION_SECRET or APP_SECRET_KEY is required "
                "to verify a live runner session"
            )
        name = f"runner-{self.state.conversation_id}"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if self.config.runtime == "docker":
                    result = subprocess.run(
                        ["docker", "inspect", name],
                        text=True,
                        capture_output=True,
                        check=True,
                    )
                    document = json.loads(result.stdout)[0]
                    identity = str(document["Id"])
                elif self.config.runtime == "kubernetes":
                    command = ["kubectl"]
                    if self.config.kubeconfig:
                        command.extend(["--kubeconfig", self.config.kubeconfig])
                    command.extend(
                        ["-n", self.config.runner_namespace, "get", "pod", name, "-o", "json"]
                    )
                    result = subprocess.run(
                        command, text=True, capture_output=True, check=True
                    )
                    document = json.loads(result.stdout)
                    identity = str(document["metadata"]["uid"])
                else:
                    raise SmokeFailure(f"unsupported runtime {self.config.runtime!r}")
                return identity, self.issue_equivalent_session_token()
            except (subprocess.CalledProcessError, KeyError, json.JSONDecodeError):
                time.sleep(1)
        raise SmokeFailure(f"runner {name} was not inspectable within 90s")

    def issue_equivalent_session_token(self) -> str:
        assert self.state.conversation_id and self.state.user_id

        def encode(value: dict[str, Any]) -> str:
            raw = json.dumps(value, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        header = encode({"alg": "HS256", "typ": "JWT"})
        payload = encode(
            {
                "sub": self.state.user_id,
                "conversation_id": self.state.conversation_id,
                "org_id": self.state.organization_id,
                "exp": int(time.time()) + 30 * 60,
                "type": "session",
            }
        )
        signing_input = f"{header}.{payload}"
        signature = hmac.new(
            self.config.session_secret.encode(),
            signing_input.encode(),
            hashlib.sha256,
        ).digest()
        encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        return f"{signing_input}.{encoded_signature}"

    def verify_live_revocation(self) -> None:
        assert self.state.skill_id and self.state.organization_id
        runner_id, token = self.create_active_runner()
        self.state.runner_identity = runner_id
        self.api("business", "GET", f"/v1/skills/{self.state.skill_id}", token)
        self.api(
            "business",
            "POST",
            "/v1/chat/completions",
            token,
            payload={
                "model": self.config.model,
                "messages": [{"role": "user", "content": "before revoke"}],
                "stream": True,
            },
            timeout=120,
        )

        self.reconcile("skill", self.state.skill_id, [])
        self.api(
            "business",
            "GET",
            f"/v1/skills/{self.state.skill_id}",
            token,
            expected=(404,),
        )
        self.reconcile("model", self.config.model, [])
        self.api(
            "business",
            "POST",
            "/v1/chat/completions",
            token,
            payload={
                "model": self.config.model,
                "messages": [{"role": "user", "content": "after revoke"}],
                "stream": True,
            },
            expected=(403,),
        )
        after_id, _ = self.runner_identity_and_token()
        if after_id != runner_id:
            raise SmokeFailure(f"runner identity changed across revoke: {runner_id} != {after_id}")

    def cleanup(self) -> None:
        log("cleaning owned fixtures")
        errors: list[str] = []

        def attempt(label: str, operation: Callable[[], object]) -> None:
            try:
                operation()
            except BaseException as exc:
                errors.append(f"{label}: {exc}")
                log(f"cleanup step failed ({label}): {exc}")

        if self.state.conversation_id and self.state.user_business_token:
            attempt(
                "conversation",
                lambda: self.api(
                    "business",
                    "DELETE",
                    f"/v1/conversations/{self.state.conversation_id}",
                    self.state.user_business_token,
                    expected=(204, 404),
                ),
            )
        if self.state.runner_identity:
            attempt("conversation runner", self.delete_owned_runner)
        if self.state.admin_admin_token:
            if self.state.model_grants is not None:
                attempt(
                    "model grants",
                    lambda: self.reconcile(
                        "model", self.config.model, self.state.model_grants or []
                    ),
                )
            if self.state.agent_id:
                attempt(
                    "agent definition",
                    lambda: self.api(
                        "admin",
                        "DELETE",
                        f"/v1/agent-definitions/{self.state.agent_id}",
                        self.state.admin_admin_token,
                        expected=(204, 404),
                    ),
                )
            if self.state.knowledge_base_id:
                attempt(
                    "knowledge base",
                    self.delete_owned_knowledge_base,
                )
            if self.state.skill_id:
                attempt(
                    "skill",
                    lambda: self.api(
                        "admin",
                        "DELETE",
                        f"/v1/skills/{self.state.skill_id}",
                        self.state.admin_admin_token,
                        expected=(204, 404),
                    ),
                )
            if self.state.org_capabilities is not None:
                attempt(
                    "organization capabilities",
                    lambda: self.put_capabilities(self.state.org_capabilities or []),
                )
            if self.state.analysts_capabilities is not None and self.state.analysts_id:
                attempt(
                    "analysts capabilities",
                    lambda: self.put_capabilities(
                        self.state.analysts_capabilities or [], self.state.analysts_id
                    ),
                )
            if self.state.engineers_capabilities is not None and self.state.engineers_id:
                attempt(
                    "engineers capabilities",
                    lambda: self.put_capabilities(
                        self.state.engineers_capabilities or [], self.state.engineers_id
                    ),
                )
        if self.state.engineers_membership_added and all(
            (
                self.state.master_token,
                self.state.keycloak_user_id,
                self.state.keycloak_engineers_id,
            )
        ):
            attempt("Keycloak engineers membership", self.remove_engineers_membership)
            attempt("membership resync", self.resync_user_membership)
        if self.state.foreign_org_id:
            attempt(
                "foreign organization",
                lambda: self.db_sql(
                    "DELETE FROM skills "
                    f"WHERE id = '{self.state.foreign_skill_id}' "
                    f"AND org_id = '{self.state.foreign_org_id}'; "
                    f"DELETE FROM organizations WHERE id = '{self.state.foreign_org_id}';"
                ),
            )
        if errors:
            raise SmokeFailure("; ".join(errors))

    def resync_user_membership(self) -> None:
        restored_token = self.token("user", "user", self.config.business_client)
        self.api("business", "GET", "/v1/auth/me", restored_token)

    def remove_engineers_membership(self) -> None:
        assert self.state.keycloak_user_id and self.state.keycloak_engineers_id
        self.state.master_token = self.master_token()
        self.keycloak_admin(
            "DELETE",
            f"/users/{self.state.keycloak_user_id}/groups/{self.state.keycloak_engineers_id}",
            expected=(204,),
        )

    def delete_owned_runner(self) -> None:
        assert self.state.conversation_id and self.state.runner_identity
        name = f"runner-{self.state.conversation_id}"
        if self.config.runtime == "docker":
            inspected = subprocess.run(
                ["docker", "inspect", name],
                text=True,
                capture_output=True,
                check=False,
            )
            if inspected.returncode != 0:
                return
            document = json.loads(inspected.stdout)[0]
            labels = document.get("Config", {}).get("Labels", {})
            if (
                str(document.get("Id")) != self.state.runner_identity
                or labels.get("aurum-ai.component") != "conversation-runner"
            ):
                raise SmokeFailure(f"refusing to remove unowned runner {name}")
            removed = subprocess.run(
                ["docker", "rm", "-f", name],
                text=True,
                capture_output=True,
                check=False,
            )
            if removed.returncode != 0:
                raise SmokeFailure(f"runner cleanup failed: {removed.stderr.strip()}")
            return

        command = ["kubectl"]
        if self.config.kubeconfig:
            command.extend(["--kubeconfig", self.config.kubeconfig])
        command.extend(
            ["-n", self.config.runner_namespace, "get", "pod", name, "-o", "json"]
        )
        inspected = subprocess.run(command, text=True, capture_output=True, check=False)
        if inspected.returncode != 0:
            return
        document = json.loads(inspected.stdout)
        labels = document.get("metadata", {}).get("labels", {})
        uid = str(document.get("metadata", {}).get("uid", ""))
        if (
            uid != self.state.runner_identity
            or labels.get("aurum-ai.component") != "conversation-runner"
        ):
            raise SmokeFailure(f"refusing to remove unowned runner Pod {name}")
        raw_command = ["kubectl"]
        if self.config.kubeconfig:
            raw_command.extend(["--kubeconfig", self.config.kubeconfig])
        raw_command.extend(
            [
                "delete",
                "--raw",
                f"/api/v1/namespaces/{self.config.runner_namespace}/pods/{name}",
                "-f",
                "-",
            ]
        )
        deleted = subprocess.run(
            raw_command,
            input=json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {"uid": uid},
                }
            ),
            text=True,
            capture_output=True,
            check=False,
        )
        if deleted.returncode != 0:
            raise SmokeFailure(f"runner Pod cleanup failed: {deleted.stderr.strip()}")

    def delete_owned_knowledge_base(self) -> None:
        assert self.state.knowledge_base_id and self.state.admin_admin_token
        try:
            self.api(
                "admin",
                "DELETE",
                f"/v1/knowledge-bases/{self.state.knowledge_base_id}",
                self.state.admin_admin_token,
                expected=(204, 404),
            )
        except SmokeFailure as exc:
            log(f"knowledge API cleanup unavailable, using owned DB fallback: {exc}")
            escaped_marker = f"{self.marker}-knowledge".replace("'", "''")
            self.db_sql(
                "DELETE FROM knowledge_bases "
                f"WHERE id = '{self.state.knowledge_base_id}' "
                f"AND name = '{escaped_marker}';"
            )

    def run(self) -> None:
        self.http.wait(f"{self.config.business_url}/health", "business backend")
        self.http.wait(f"{self.config.admin_url}/health", "admin backend")
        self.http.wait(
            f"{self.config.keycloak_url}/realms/{self.config.realm}", "Keycloak realm"
        )
        self.acquire_tokens()
        self.sync_users()
        self.add_second_group_membership()
        self.load_subjects()
        self.configure_capabilities()
        self.create_resources()
        self.configure_grants()
        self.seed_foreign_resource()
        self.verify_access_matrix()
        self.verify_preview()
        self.verify_live_revocation()
        log("all Authorization v2 acceptance checks passed")


def main() -> int:
    acceptance = Acceptance(Config())
    failure: BaseException | None = None
    try:
        acceptance.run()
    except BaseException as exc:
        failure = exc
        log(f"FAILED: {exc}")
    finally:
        try:
            acceptance.cleanup()
        except BaseException as cleanup_exc:
            log(f"cleanup failed: {cleanup_exc}")
            if failure is None:
                failure = cleanup_exc
    return 1 if failure is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
