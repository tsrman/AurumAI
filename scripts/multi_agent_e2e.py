#!/usr/bin/env python3
"""Owned-resource multi-agent acceptance for Compose and Kubernetes.

Coverage markers used by the static contract: default-deny, audience separation,
exactly one final message, safe SSE, live revoke, quota exhausted, cancelled,
and ownership marker cleanup. Credentials and HTTP bodies are never logged.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
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
    print(f"[multi-agent-smoke] {message}", file=sys.stderr, flush=True)


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
    model: str = field(
        default_factory=lambda: os.getenv("MULTI_AGENT_E2E_MODEL", "gpt-4o-mini")
    )
    runtime: str = field(
        default_factory=lambda: os.getenv("MULTI_AGENT_E2E_RUNTIME", "docker")
    )
    db_container: str = field(
        default_factory=lambda: os.getenv("MULTI_AGENT_E2E_DB_CONTAINER", "aurum-ai-postgres")
    )
    kubeconfig: str | None = field(
        default_factory=lambda: os.getenv("MULTI_AGENT_E2E_KUBECONFIG")
    )
    runner_namespace: str = field(
        default_factory=lambda: os.getenv(
            "MULTI_AGENT_E2E_RUNNER_NAMESPACE", "aurum-ai-runners"
        )
    )


@dataclass
class State:
    admin_business_token: str = ""
    admin_admin_token: str = ""
    member_business_token: str = ""
    member_admin_token: str = ""
    org_id: str = ""
    member_id: str = ""
    original_capabilities: list[str] = field(default_factory=list)
    original_quota_requests: str | None = None
    quota_snapshot_taken: bool = False
    agent_ids: dict[str, str] = field(default_factory=dict)
    workflow_ids: dict[str, str] = field(default_factory=dict)
    grant_ids: list[str] = field(default_factory=list)
    agent_member_grants: dict[str, str] = field(default_factory=dict)
    conversation_id: str | None = None
    conversation_ids: list[str] = field(default_factory=list)
    runner_identity: str | None = None


class Acceptance:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = State()
        self.marker = f"multi-agent-e2e-{uuid.uuid4().hex[:10]}"

    def request(
        self,
        plane: str,
        method: str,
        path: str,
        token: str,
        *,
        body: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
        timeout: float = 60,
    ) -> tuple[int, Any]:
        base = self.config.business_url if plane == "business" else self.config.admin_url
        headers = {"Accept": "application/json"}
        data = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = urllib.request.Request(
            f"{base}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
        except OSError as exc:
            raise SmokeFailure(f"{method} {path}: transport failed") from exc
        parsed: Any = None
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw.decode(errors="replace")
        if status not in expected:
            raise SmokeFailure(f"{method} {path}: expected {expected}, got {status}")
        return status, parsed

    def token(self, username: str, password: str, client: str) -> str:
        data = urllib.parse.urlencode(
            {
                "grant_type": "password",
                "client_id": client,
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
                result = json.loads(response.read())
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise SmokeFailure("Keycloak token acquisition failed") from exc
        return str(result["access_token"])

    def acquire_tokens(self) -> None:
        self.state.admin_business_token = self.token(
            "admin", "admin", self.config.business_client
        )
        self.state.admin_admin_token = self.token("admin", "admin", self.config.admin_client)
        self.state.member_business_token = self.token(
            "user", "user", self.config.business_client
        )
        self.state.member_admin_token = self.token("user", "user", self.config.admin_client)
        for token in (self.state.admin_business_token, self.state.member_business_token):
            self.request("business", "GET", "/v1/auth/me", token)

    def load_subjects_and_capabilities(self) -> None:
        _, response = self.request(
            "admin",
            "GET",
            "/v1/admin/authorization/subjects",
            self.state.admin_admin_token,
        )
        subjects = response["data"]
        organizations = [item for item in subjects if item["principalType"] == "organization"]
        members = [
            item
            for item in subjects
            if item["principalType"] == "user" and item["label"] == "Regular User"
        ]
        if len(organizations) != 1 or len(members) != 1:
            raise SmokeFailure("authorization subjects are not provisioned as expected")
        self.state.org_id = str(organizations[0]["principalId"])
        self.state.member_id = str(members[0]["principalId"])
        _, response = self.request(
            "admin",
            "GET",
            "/v1/admin/authorization/capabilities/organization",
            self.state.admin_admin_token,
        )
        self.state.original_capabilities = list(response["data"]["capabilities"])
        capabilities = sorted(
            set(self.state.original_capabilities)
            | {"chat.send", "workspace.agents.create"}
        )
        self.request(
            "admin",
            "PUT",
            "/v1/admin/authorization/capabilities/organization",
            self.state.admin_admin_token,
            body={"capabilities": capabilities},
        )

    def verify_admin_boundaries(self) -> None:
        # Admin-only CRUD and strict audience separation.
        self.request(
            "admin",
            "GET",
            "/v1/admin/workflows",
            self.state.member_admin_token,
            expected=(403,),
        )
        self.request(
            "admin",
            "GET",
            "/v1/admin/workflows",
            self.state.admin_business_token,
            expected=(401,),
        )
        self.request(
            "business",
            "GET",
            "/v1/workflows",
            self.state.admin_admin_token,
            expected=(401,),
        )

    def create_agents(self) -> None:
        for key in ("supervisor", "alpha", "beta"):
            _, response = self.request(
                "admin",
                "POST",
                "/v1/agent-definitions",
                self.state.admin_admin_token,
                body={
                    "name": f"{self.marker}-{key}",
                    "description": "owned multi-agent acceptance fixture",
                    "modelId": self.config.model,
                    "systemPrompt": f"Deterministic {key} fixture.",
                    "temperature": 0,
                    "skillIds": [],
                    "knowledgeBaseIds": [],
                },
                expected=(201,),
            )
            self.state.agent_ids[key] = str(response["data"]["id"])

    def workflow_body(
        self,
        name: str,
        *,
        parallel: bool,
        barrier: str | None = None,
    ) -> dict[str, Any]:
        alpha_instruction = "MULTI_AGENT_NODE:alpha"
        if barrier:
            alpha_instruction += f" MULTI_AGENT_BARRIER:{barrier}"
        return {
            "name": f"{self.marker}-{name}",
            "description": "owned multi-agent acceptance fixture",
            "supervisorAgentDefinitionId": self.state.agent_ids["supervisor"],
            "maxParallelism": 2 if parallel else 1,
            "nodes": [
                {
                    "nodeKey": "alpha",
                    "displayName": "Alpha",
                    "agentDefinitionId": self.state.agent_ids["alpha"],
                    "instruction": alpha_instruction,
                },
                {
                    "nodeKey": "beta",
                    "displayName": "Beta",
                    "agentDefinitionId": self.state.agent_ids["beta"],
                    "instruction": "MULTI_AGENT_NODE:beta",
                },
            ],
            "edges": []
            if parallel
            else [{"sourceNodeKey": "alpha", "targetNodeKey": "beta"}],
        }

    def create_workflows(self) -> None:
        definitions = {
            "sequential": self.workflow_body("sequential", parallel=False),
            "parallel": self.workflow_body("parallel", parallel=True),
            "gated": self.workflow_body("gated", parallel=False, barrier="boundary"),
        }
        for key, body in definitions.items():
            self.request(
                "admin",
                "POST",
                "/v1/admin/workflows/validate",
                self.state.admin_admin_token,
                body=body,
            )
            _, response = self.request(
                "admin",
                "POST",
                "/v1/admin/workflows",
                self.state.admin_admin_token,
                body=body,
                expected=(201,),
            )
            self.state.workflow_ids[key] = str(response["data"]["id"])

    def create_grant(self, resource_type: str, resource_id: str) -> str:
        _, response = self.request(
            "admin",
            "POST",
            "/v1/admin/authorization/grants",
            self.state.admin_admin_token,
            body={
                "resourceType": resource_type,
                "resourceId": resource_id,
                "principalType": "user",
                "principalId": self.state.member_id,
                "permission": "read",
            },
            expected=(201,),
        )
        grant_id = str(response["data"]["id"])
        self.state.grant_ids.append(grant_id)
        return grant_id

    def grant_member_access(self) -> None:
        # Verify default-deny before granting the workflow and its dependencies.
        first_workflow = next(iter(self.state.workflow_ids.values()))
        self.request(
            "business",
            "GET",
            f"/v1/workflows/{first_workflow}",
            self.state.member_business_token,
            expected=(404,),
        )
        for key, agent_id in self.state.agent_ids.items():
            grant_id = self.create_grant("agent_definition", agent_id)
            self.state.agent_member_grants[key] = grant_id
        for workflow_id in self.state.workflow_ids.values():
            self.create_grant("workflow", workflow_id)
        _, response = self.request(
            "business", "GET", "/v1/models", self.state.member_business_token
        )
        if self.config.model not in {item["id"] for item in response["data"]}:
            self.create_grant("model", self.config.model)
            _, response = self.request(
                "business", "GET", "/v1/models", self.state.member_business_token
            )
            if self.config.model not in {item["id"] for item in response["data"]}:
                raise SmokeFailure("member cannot read the configured workflow model")

    def create_conversation(self) -> str:
        _, response = self.request(
            "business",
            "POST",
            "/v1/conversations",
            self.state.member_business_token,
            body={"title": self.marker, "model": self.config.model},
            expected=(200, 201),
        )
        conversation_id = str(response["data"]["id"])
        self.state.conversation_ids.append(conversation_id)
        return conversation_id

    def stream_events(
        self,
        conversation_id: str,
        workflow_id: str,
        on_event: Any | None = None,
    ) -> list[dict[str, Any]]:
        body = json.dumps(
            {
                "conversation_id": conversation_id,
                "messages": [{"role": "user", "content": f"{self.marker} request"}],
                "model": self.config.model,
                "workflowId": workflow_id,
            }
        ).encode()
        request = urllib.request.Request(
            f"{self.config.business_url}/v1/chat/stream",
            data=body,
            headers={
                "Authorization": f"Bearer {self.state.member_business_token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        events: list[dict[str, Any]] = []
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                event_name: str | None = None
                for raw_line in response:
                    line = raw_line.decode().rstrip("\r\n")
                    if line.startswith("event: "):
                        event_name = line[7:]
                    elif line.startswith("data: ") and event_name:
                        event = {"type": event_name, "data": json.loads(line[6:])}
                        events.append(event)
                        if on_event is not None:
                            on_event(event)
                        event_name = None
        except urllib.error.HTTPError as exc:
            raise SmokeFailure(f"workflow stream returned HTTP {exc.code}") from exc
        except OSError as exc:
            raise SmokeFailure("workflow stream transport failed") from exc
        return events

    @staticmethod
    def assert_safe_events(events: list[dict[str, Any]]) -> None:
        allowed = {
            "workflow-run-status",
            "workflow-node-status",
            "message-end",
            "done",
            "message-score",
        }
        if not events or any(event["type"] not in allowed for event in events):
            event_types = sorted({str(event.get("type")) for event in events})
            raise SmokeFailure(
                f"safe SSE contract was violated (event types: {event_types})"
            )
        run_keys = {"turnId", "workflowRunId", "status", "errorCode"}
        node_keys = run_keys | {"nodeKey", "nodeName"}
        for event in events:
            if event["type"] == "workflow-run-status" and not set(event["data"]) <= run_keys:
                raise SmokeFailure("workflow run SSE contains non-allowlisted fields")
            if event["type"] == "workflow-node-status" and not set(event["data"]) <= node_keys:
                raise SmokeFailure("workflow node SSE contains non-allowlisted fields")

    def assert_single_final(
        self, conversation_id: str, marker: str, expected_assistant_count: int
    ) -> None:
        _, response = self.request(
            "business",
            "GET",
            f"/v1/conversations/{conversation_id}",
            self.state.member_business_token,
        )
        messages = response["data"]["messages"]["data"]
        assistants = [item for item in messages if item["role"] == "assistant"]
        if (
            len(assistants) != expected_assistant_count
            or not assistants
            or assistants[-1].get("content") != marker
        ):
            raise SmokeFailure("exactly one final message was not persisted")
        child_markers = [
            item
            for item in messages
            if item["role"] == "assistant"
            and str(item.get("content", "")).startswith("MULTI_AGENT_NODE_RESULT:")
        ]
        if child_markers:
            raise SmokeFailure("raw child output leaked into conversation history")

    def run_sequential(self) -> None:
        conversation_id = self.create_conversation()
        self.state.conversation_id = conversation_id
        events = self.stream_events(conversation_id, self.state.workflow_ids["sequential"])
        self.assert_safe_events(events)
        statuses = [
            event["data"]["status"]
            for event in events
            if event["type"] == "workflow-run-status"
        ]
        if "completed" not in statuses:
            raise SmokeFailure("sequential workflow did not complete")
        self.assert_single_final(
            conversation_id, "MULTI_AGENT_FINAL:alpha,beta", 1
        )
        self.state.runner_identity = self.runner_identity(conversation_id)
        log("sequential workflow passed")

    def run_parallel(self) -> None:
        assert self.state.conversation_id and self.state.runner_identity
        events = self.stream_events(
            self.state.conversation_id, self.state.workflow_ids["parallel"]
        )
        self.assert_safe_events(events)
        running_nodes = {
            event["data"]["nodeKey"]
            for event in events
            if event["type"] == "workflow-node-status"
            and event["data"]["status"] == "running"
        }
        if running_nodes != {"alpha", "beta"}:
            raise SmokeFailure("parallel workflow did not start both nodes")
        if self.runner_identity(self.state.conversation_id) != self.state.runner_identity:
            raise SmokeFailure("parallel child workflows created a second runner")
        self.assert_single_final(
            self.state.conversation_id, "MULTI_AGENT_FINAL:alpha,beta", 2
        )
        log("parallel workflow and runner reuse passed")

    def start_stream(self, conversation_id: str, workflow_id: str) -> tuple[queue.Queue[Any], threading.Thread]:
        output: queue.Queue[Any] = queue.Queue()

        def target() -> None:
            try:
                self.stream_events(conversation_id, workflow_id, output.put)
            except BaseException as exc:  # propagated to the controlling thread
                output.put(exc)
            finally:
                output.put(None)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return output, thread

    @staticmethod
    def wait_for_event(
        output: queue.Queue[Any],
        events: list[dict[str, Any]],
        predicate: Any,
        timeout: float = 90,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                item = output.get(timeout=min(1, deadline - time.monotonic()))
            except queue.Empty:
                continue
            if isinstance(item, BaseException):
                raise SmokeFailure("background workflow stream failed") from item
            if item is None:
                break
            events.append(item)
            if predicate(item):
                return item
        raise SmokeFailure("timed out waiting for workflow boundary event")

    @staticmethod
    def drain_stream(
        output: queue.Queue[Any], thread: threading.Thread, events: list[dict[str, Any]]
    ) -> None:
        while True:
            item = output.get(timeout=180)
            if isinstance(item, BaseException):
                raise SmokeFailure("background workflow stream failed") from item
            if item is None:
                break
            events.append(item)
        thread.join(timeout=5)
        if thread.is_alive():
            raise SmokeFailure("background workflow stream did not stop")

    def delete_grant(self, grant_id: str) -> None:
        self.request(
            "admin",
            "DELETE",
            f"/v1/admin/authorization/grants/{grant_id}",
            self.state.admin_admin_token,
            expected=(204,),
        )
        if grant_id in self.state.grant_ids:
            self.state.grant_ids.remove(grant_id)

    def restore_agent_grant(self, key: str) -> None:
        grant_id = self.create_grant("agent_definition", self.state.agent_ids[key])
        self.state.agent_member_grants[key] = grant_id

    def run_live_revoke(self) -> None:
        conversation_id = self.create_conversation()
        output, thread = self.start_stream(conversation_id, self.state.workflow_ids["gated"])
        events: list[dict[str, Any]] = []
        self.wait_for_event(
            output,
            events,
            lambda event: event["type"] == "workflow-node-status"
            and event["data"]["nodeKey"] == "alpha"
            and event["data"]["status"] == "running",
        )
        self.delete_grant(self.state.agent_member_grants["beta"])
        self.drain_stream(output, thread, events)
        self.assert_safe_events(events)
        statuses = [
            event["data"]
            for event in events
            if event["type"] == "workflow-run-status"
        ]
        if not any(
            item["status"] == "failed" and item.get("errorCode") == "access_revoked"
            for item in statuses
        ):
            trace = [
                f"{item['status']}:{item.get('errorCode', '-')}" for item in statuses
            ]
            raise SmokeFailure(
                "live revoke did not fail with access_revoked "
                f"(workflow status trace: {trace})"
            )
        if any(item["status"] == "synthesizing" for item in statuses):
            raise SmokeFailure("synthesis started after live revoke")
        self.restore_agent_grant("beta")
        log("live revoke passed")

    def db(self, sql: str) -> str:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                self.config.db_container,
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
            raise SmokeFailure("owned database operation failed")
        return result.stdout.strip()

    @staticmethod
    def assert_quota_exhaustion(events: list[dict[str, Any]]) -> None:
        statuses = [
            event["data"]
            for event in events
            if event["type"] == "workflow-run-status"
        ]
        if not any(
            item["status"] == "failed" and item.get("errorCode") == "quota_exhausted"
            for item in statuses
        ):
            raise SmokeFailure("quota exhausted scenario did not fail safely")
        running_nodes = {
            event["data"]["nodeKey"]
            for event in events
            if event["type"] == "workflow-node-status"
            and event["data"]["status"] == "running"
        }
        if "beta" in running_nodes:
            raise SmokeFailure("the next agent started after quota exhaustion")

    def run_quota_exhaustion(self) -> None:
        original = self.db(
            f"SELECT COALESCE(quota_requests::text, 'NULL') FROM organizations "
            f"WHERE id='{self.state.org_id}'::uuid;"
        )
        self.state.original_quota_requests = None if original == "NULL" else original
        self.state.quota_snapshot_taken = True
        used_raw = self.db(
            "SELECT COALESCE(requests_used, 0)::text FROM organization_usage_monthly "
            f"WHERE org_id='{self.state.org_id}'::uuid "
            "AND period_start=date_trunc('month', now())::date;"
        )
        used = int(used_raw or "0")
        self.db(
            f"UPDATE organizations SET quota_requests={used + 1} "
            f"WHERE id='{self.state.org_id}'::uuid;"
        )
        try:
            events = self.stream_events(
                self.create_conversation(), self.state.workflow_ids["sequential"]
            )
            self.assert_quota_exhaustion(events)
        finally:
            self.restore_quota()
        log("quota exhaustion passed")

    def restore_quota(self) -> None:
        if not self.state.quota_snapshot_taken or not self.state.org_id:
            return
        if self.state.original_quota_requests is None:
            value = "NULL"
        else:
            value = self.state.original_quota_requests
        self.db(
            f"UPDATE organizations SET quota_requests={value} "
            f"WHERE id='{self.state.org_id}'::uuid;"
        )
        self.state.original_quota_requests = None
        self.state.quota_snapshot_taken = False

    def run_cancellation(self) -> None:
        conversation_id = self.create_conversation()
        output, thread = self.start_stream(conversation_id, self.state.workflow_ids["gated"])
        events: list[dict[str, Any]] = []
        boundary = self.wait_for_event(
            output,
            events,
            lambda event: event["type"] == "workflow-node-status"
            and event["data"]["nodeKey"] == "alpha"
            and event["data"]["status"] == "running",
        )
        run_id = str(boundary["data"]["workflowRunId"])
        self.request(
            "business",
            "POST",
            f"/v1/workflow-runs/{run_id}/cancel",
            self.state.member_business_token,
            expected=(202,),
        )
        self.drain_stream(output, thread, events)
        if not any(
            event["type"] == "workflow-run-status"
            and event["data"]["status"] == "cancelled"
            for event in events
        ):
            raise SmokeFailure("cancelled workflow did not emit its terminal status")
        _, response = self.request(
            "business",
            "GET",
            f"/v1/conversations/{conversation_id}",
            self.state.member_business_token,
        )
        if any(item["role"] == "assistant" for item in response["data"]["messages"]["data"]):
            raise SmokeFailure("cancelled workflow persisted an assistant message")
        log("exact-run cancellation passed")

    def runner_identity(self, conversation_id: str) -> str:
        label = f"aurum-ai.conversation={conversation_id}"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.config.runtime == "kubernetes":
                command = ["kubectl"]
                if self.config.kubeconfig:
                    command.extend(["--kubeconfig", self.config.kubeconfig])
                command.extend(
                    [
                        "-n",
                        self.config.runner_namespace,
                        "get",
                        "pods",
                        "-l",
                        label,
                        "-o",
                        "json",
                    ]
                )
                result = subprocess.run(command, text=True, capture_output=True, check=False)
                if result.returncode == 0:
                    items = json.loads(result.stdout).get("items", [])
                    if len(items) == 1:
                        return str(items[0]["metadata"]["uid"])
                    if len(items) > 1:
                        raise SmokeFailure("more than one runner Pod has the conversation label")
            else:
                result = subprocess.run(
                    ["docker", "inspect", f"runner-{conversation_id}"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if result.returncode == 0:
                    documents = json.loads(result.stdout)
                    if len(documents) == 1:
                        return str(documents[0]["Id"])
            time.sleep(1)
        raise SmokeFailure("conversation runner identity was not observable")

    def cleanup_owned_resources(self) -> None:
        # Database cleanup is scoped by the unique ownership marker because the
        # public API archives workflows and intentionally prevents deleting
        # referenced agents.
        for conversation_id in self.state.conversation_ids:
            if self.state.member_business_token:
                self.request(
                    "business",
                    "DELETE",
                    f"/v1/conversations/{conversation_id}",
                    self.state.member_business_token,
                    expected=(204,),
                )
        for grant_id in list(self.state.grant_ids):
            if self.state.admin_admin_token:
                self.request(
                    "admin",
                    "DELETE",
                    f"/v1/admin/authorization/grants/{grant_id}",
                    self.state.admin_admin_token,
                    expected=(204, 404),
                )
        self.state.grant_ids.clear()
        if self.state.org_id:
            escaped = self.marker.replace("'", "''")
            self.db(
                "DELETE FROM access_grants "
                f"WHERE org_id='{self.state.org_id}'::uuid AND ((resource_type='workflow' "
                "AND resource_id IN (SELECT id::text FROM workflow_definitions "
                f"WHERE org_id='{self.state.org_id}'::uuid AND name LIKE '{escaped}-%')) "
                "OR (resource_type='agent_definition' AND resource_id IN "
                "(SELECT id::text FROM agent_definitions "
                f"WHERE org_id='{self.state.org_id}'::uuid AND name LIKE '{escaped}-%')));\n"
                "UPDATE llm_usage_events SET workflow_run_id=NULL, "
                "workflow_phase=NULL, workflow_node_key=NULL WHERE workflow_run_id IN "
                "(SELECT wr.id FROM workflow_runs wr JOIN workflow_definitions wd "
                "ON wd.id=wr.workflow_definition_id AND wd.org_id=wr.org_id "
                f"WHERE wd.org_id='{self.state.org_id}'::uuid AND wd.name LIKE '{escaped}-%');\n"
                "DELETE FROM workflow_node_runs WHERE workflow_run_id IN "
                "(SELECT wr.id FROM workflow_runs wr JOIN workflow_definitions wd "
                "ON wd.id=wr.workflow_definition_id AND wd.org_id=wr.org_id "
                f"WHERE wd.org_id='{self.state.org_id}'::uuid AND wd.name LIKE '{escaped}-%');\n"
                "DELETE FROM workflow_runs WHERE workflow_definition_id IN "
                "(SELECT id FROM workflow_definitions "
                f"WHERE org_id='{self.state.org_id}'::uuid AND name LIKE '{escaped}-%');\n"
                f"DELETE FROM workflow_definitions WHERE org_id='{self.state.org_id}'::uuid "
                f"AND name LIKE '{escaped}-%';\n"
                f"DELETE FROM agent_definitions WHERE org_id='{self.state.org_id}'::uuid "
                f"AND name LIKE '{escaped}-%';\n"
                f"DELETE FROM conversations WHERE org_id='{self.state.org_id}'::uuid "
                f"AND title='{escaped}';"
            )

    def restore_configuration(self) -> None:
        if self.state.org_id:
            try:
                self.restore_quota()
            except SmokeFailure:
                pass
        if self.state.admin_admin_token and self.state.original_capabilities:
            try:
                self.request(
                    "admin",
                    "PUT",
                    "/v1/admin/authorization/capabilities/organization",
                    self.state.admin_admin_token,
                    body={"capabilities": self.state.original_capabilities},
                )
            except SmokeFailure:
                pass

    def run(self) -> None:
        try:
            self.acquire_tokens()
            self.load_subjects_and_capabilities()
            self.verify_admin_boundaries()
            self.create_agents()
            self.create_workflows()
            self.grant_member_access()
            self.run_sequential()
            self.run_parallel()
            self.run_live_revoke()
            self.run_quota_exhaustion()
            self.run_cancellation()
            log("all multi-agent scenarios passed")
        finally:
            self.restore_configuration()
            try:
                self.cleanup_owned_resources()
            except SmokeFailure:
                log("warning: owned fixture cleanup failed")


def main() -> int:
    try:
        Acceptance(Config()).run()
    except SmokeFailure as exc:
        log(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
