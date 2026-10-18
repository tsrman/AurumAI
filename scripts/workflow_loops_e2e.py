#!/usr/bin/env python3
"""API-only loop acceptance under the C5 wrapper's exact-owned recovery journal.

The wrapper prepares and recovers resources, pins Temporal executions before
metadata cleanup, preserves billing/history, and retains its journal on failure.
This module never independently deletes resources or manages Compose.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


from workflow_templates_ui_e2e import Config as RecoveryConfig
from workflow_templates_ui_e2e import FixtureController, FixtureError


class SmokeFailure(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[workflow-loops-smoke] {message}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Config(RecoveryConfig):
    business_client: str = "business-ui"
    admin_client: str = "admin-ui"

    @property
    def model(self) -> str:
        return f"{os.environ['E2E_WORKFLOW_TEMPLATES_UI_MARKER']}-model"


@dataclass
class State:
    member_business_token: str = ""
    admin_admin_token: str = ""
    agent_ids: dict[str, str] = field(default_factory=dict)
    workflow_ids: dict[str, str] = field(default_factory=dict)
    conversation_ids: list[str] = field(default_factory=list)


class Acceptance:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = State()
        path = os.getenv("E2E_WORKFLOW_TEMPLATES_UI_STATE")
        marker = os.getenv("E2E_WORKFLOW_TEMPLATES_UI_MARKER")
        if not path or not marker or os.getenv("WORKFLOW_ACCEPTANCE_KIND") != "loops":
            raise SmokeFailure("loop acceptance requires the C5-managed recovery journal")
        try:
            self.recovery = FixtureController(config, Path(path), marker)
            self.fixture = self.recovery._state()
        except (FixtureError, OSError, ValueError) as exc:
            raise SmokeFailure("invalid loop recovery journal") from exc
        if (
            not self.fixture.prepared
            or not self.fixture.source_workflow_id
            or set(self.fixture.source_agent_ids) != {"v1", "v2"}
        ):
            raise SmokeFailure("loop recovery fixture is not prepared")
        self.marker = marker
        self.state.agent_ids = {
            "supervisor": self.fixture.source_agent_ids["v1"],
            "start": self.fixture.source_agent_ids["v1"],
            "alpha": self.fixture.source_agent_ids["v2"],
        }

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
        c = self.config
        self.state.member_business_token = self.token(
            c.member_username, c.member_password, c.business_client
        )
        self.state.admin_admin_token = self.token(
            c.admin_username, c.admin_password, c.admin_client
        )
        for plane, token, user_id in (
            ("business", self.state.member_business_token, self.fixture.member_id),
            ("admin", self.state.admin_admin_token, self.fixture.admin_id),
        ):
            _, identity = self.request(plane, "GET", "/v1/auth/me", token)
            identity = identity["data"]
            if (
                identity.get("user_id") != user_id
                or identity.get("org_id") != self.fixture.organization_id
            ):
                raise SmokeFailure("live identity does not match recovery journal")
        for resource_type, resource_id in (
            ("model", c.model),
            ("agent_definition", self.state.agent_ids["start"]),
            ("agent_definition", self.state.agent_ids["alpha"]),
            ("workflow", self.fixture.source_workflow_id),
        ):
            self.request(
                "admin", "POST", "/v1/admin/authorization/grants",
                self.state.admin_admin_token,
                body={
                    "resourceType": resource_type, "resourceId": resource_id,
                    "principalType": "user", "principalId": self.fixture.member_id,
                    "permission": "read",
                },
                expected=(201,),
            )

    def create_workflow(self, key: str, body: dict[str, Any]) -> str:
        # Reuse only the prepared definition; its exact name is a recovery proof.
        body = {**body, "name": f"{self.marker}-source", "autoRoutable": False}
        self.request(
            "admin", "POST", "/v1/admin/workflows/validate",
            self.state.admin_admin_token, body=body,
        )
        workflow_id = self.fixture.source_workflow_id
        _, current = self.request(
            "admin", "GET", f"/v1/admin/workflows/{workflow_id}",
            self.state.admin_admin_token,
        )
        self.request(
            "admin", "PUT", f"/v1/admin/workflows/{workflow_id}",
            self.state.admin_admin_token, body={**body, "version": current["data"]["version"]},
        )
        self.state.workflow_ids[key] = workflow_id
        return workflow_id

    def create_conversation(self) -> str:
        _, response = self.request(
            "business",
            "POST",
            "/v1/conversations",
            self.state.member_business_token,
            body={"title": f"{self.marker}-run-v{len(self.state.conversation_ids) + 1}", "model": self.config.model},
            expected=(200, 201),
        )
        conversation_id = str(response["data"]["id"])
        self.recovery.record("conversation", conversation_id)
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

    def get_run(self, run_id: str) -> dict[str, Any]:
        _, response = self.request(
            "business",
            "GET",
            f"/v1/workflow-runs/{run_id}",
            self.state.member_business_token,
        )
        return dict(response["data"])

    def loop_body(self, key: str) -> dict[str, Any]:
        # start -> alpha (structural, on_success) makes "start" a structural
        # ancestor of "alpha"; alpha -> start (isLoopBack, on_failure) closes
        # the loop back to it. alpha always fails deterministically
        # (MULTI_AGENT_FAIL:alpha), so the loop re-enters on every iteration
        # until max_iterations is exhausted, then falls through — alpha's
        # failure is "handled" (it's the loop's own tail with an on_failure
        # condition), so the run completes using only "start"'s output.
        return {
            "name": f"{self.marker}-{key}",
            "description": "owned workflow-loops acceptance fixture",
            "supervisorAgentDefinitionId": self.state.agent_ids["supervisor"],
            "maxParallelism": 1,
            "nodes": [
                {
                    "nodeKey": "start",
                    "displayName": "Start",
                    "agentDefinitionId": self.state.agent_ids["start"],
                    "instruction": "MULTI_AGENT_NODE:start",
                },
                {
                    "nodeKey": "alpha",
                    "displayName": "Alpha",
                    "agentDefinitionId": self.state.agent_ids["alpha"],
                    "instruction": "MULTI_AGENT_FAIL:alpha\nMULTI_AGENT_NODE:alpha",
                },
            ],
            "edges": [
                {"sourceNodeKey": "start", "targetNodeKey": "alpha", "condition": "on_success"},
                {
                    "sourceNodeKey": "alpha",
                    "targetNodeKey": "start",
                    "condition": "on_failure",
                    "isLoopBack": True,
                    "maxIterations": 3,
                },
            ],
        }

    def cancel_loop_body(self, key: str) -> dict[str, Any]:
        # Same start->alpha->start shape as loop_body, but "start" also
        # carries a barrier marker: the runner publishes its "running"
        # status *before* calling the gateway, so the barrier's sleep
        # (TEST_GATEWAY_BARRIER_SECONDS) gives the harness a deterministic,
        # multi-second window to cancel mid-iteration instead of racing a
        # handful of local activity round-trips. The marker can't live on
        # "alpha" — the gateway's MULTI_AGENT_FAIL check raises before it
        # ever inspects the barrier marker, so a node with both never
        # sleeps. max_iterations=5 gives more headroom than the 2 iterations
        # this scenario needs, so cancellation definitely lands before the
        # loop would exhaust on its own.
        return {
            "name": f"{self.marker}-{key}",
            "description": "owned workflow-loops acceptance fixture",
            "supervisorAgentDefinitionId": self.state.agent_ids["supervisor"],
            "maxParallelism": 1,
            "nodes": [
                {
                    "nodeKey": "start",
                    "displayName": "Start",
                    "agentDefinitionId": self.state.agent_ids["start"],
                    "instruction": "MULTI_AGENT_NODE:start\nMULTI_AGENT_BARRIER:loops-cancel",
                },
                {
                    "nodeKey": "alpha",
                    "displayName": "Alpha",
                    "agentDefinitionId": self.state.agent_ids["alpha"],
                    "instruction": "MULTI_AGENT_FAIL:alpha\nMULTI_AGENT_NODE:alpha",
                },
            ],
            "edges": [
                {"sourceNodeKey": "start", "targetNodeKey": "alpha", "condition": "on_success"},
                {
                    "sourceNodeKey": "alpha",
                    "targetNodeKey": "start",
                    "condition": "on_failure",
                    "isLoopBack": True,
                    "maxIterations": 5,
                },
            ],
        }

    def previous_iteration_context_body(self, key: str) -> dict[str, Any]:
        return {
            "name": f"{self.marker}-{key}",
            "description": "owned previous-iteration context fixture",
            "supervisorAgentDefinitionId": self.state.agent_ids["supervisor"],
            "maxParallelism": 1,
            "nodes": [
                {
                    "nodeKey": "draft",
                    "displayName": "Draft",
                    "agentDefinitionId": self.state.agent_ids["start"],
                    "instruction": "MULTI_AGENT_LOOP_CONTEXT:draft",
                },
                {
                    "nodeKey": "review",
                    "displayName": "Review",
                    "agentDefinitionId": self.state.agent_ids["alpha"],
                    "instruction": "MULTI_AGENT_LOOP_CONTEXT:review",
                },
            ],
            "edges": [
                {
                    "sourceNodeKey": "draft",
                    "targetNodeKey": "review",
                    "condition": "on_success",
                },
                {
                    "sourceNodeKey": "review",
                    "targetNodeKey": "draft",
                    "condition": "on_success",
                    "isLoopBack": True,
                    "maxIterations": 2,
                },
            ],
        }

    def run_iteration_limit_and_fallthrough(self) -> None:
        self.create_workflow("loop", self.loop_body("loop"))
        conversation_id = self.create_conversation()
        events = self.stream_events(conversation_id, self.state.workflow_ids["loop"])
        self.assert_safe_events(events)

        run_statuses = [
            event["data"]["status"]
            for event in events
            if event["type"] == "workflow-run-status"
        ]
        if run_statuses[-1] != "completed":
            raise SmokeFailure(
                f"loop exhaustion did not fall through to completion (trace: {run_statuses})"
            )
        run_id = next(
            event["data"]["workflowRunId"]
            for event in events
            if event["type"] == "workflow-run-status"
        )
        run = self.get_run(run_id)
        nodes_by_key = {node["phaseKey"]: node for node in run["nodes"]}
        start_node = nodes_by_key["start"]
        alpha_node = nodes_by_key["alpha"]
        if start_node["status"] != "completed" or start_node["attemptCount"] != 3:
            raise SmokeFailure(
                f"start node did not re-run for every iteration (node: {start_node})"
            )
        if alpha_node["status"] != "failed" or alpha_node["attemptCount"] != 3:
            raise SmokeFailure(
                f"alpha node did not exhaust its iteration cap (node: {alpha_node})"
            )
        if alpha_node["errorCode"] != "execution_failed":
            raise SmokeFailure(
                f"alpha node's terminal error code was not stable (node: {alpha_node})"
            )
        _, response = self.request(
            "business",
            "GET",
            f"/v1/conversations/{conversation_id}",
            self.state.member_business_token,
        )
        messages = response["data"]["messages"]["data"]
        assistants = [item for item in messages if item["role"] == "assistant"]
        if len(assistants) != 1 or assistants[-1].get("content") != "MULTI_AGENT_FINAL:start":
            raise SmokeFailure(
                "final answer was not synthesized from only the succeeding branch "
                f"(assistants: {assistants})"
            )
        log(f"iteration limit and fallthrough passed (3 iterations, run {run_id})")

    def run_previous_iteration_context(self) -> None:
        self.create_workflow(
            "previous-context",
            self.previous_iteration_context_body("previous-context"),
        )
        conversation_id = self.create_conversation()
        events = self.stream_events(
            conversation_id, self.state.workflow_ids["previous-context"]
        )
        self.assert_safe_events(events)
        run_id = next(
            event["data"]["workflowRunId"]
            for event in events
            if event["type"] == "workflow-run-status"
        )
        run = self.get_run(run_id)
        nodes_by_key = {node["phaseKey"]: node for node in run["nodes"]}
        for node_key in ("draft", "review"):
            node = nodes_by_key[node_key]
            if node["status"] != "completed" or node["attemptCount"] != 2:
                raise SmokeFailure(
                    f"{node_key} did not complete exactly two iterations (node: {node})"
                )
        _, response = self.request(
            "business", "GET", f"/v1/conversations/{conversation_id}",
            self.state.member_business_token,
        )
        assistants = [
            item for item in response["data"]["messages"]["data"]
            if item["role"] == "assistant"
        ]
        expected = "MULTI_AGENT_LOOP_CONTEXT_FINAL:draft-v2-from-draft-v1"
        if len(assistants) != 1 or assistants[-1].get("content") != expected:
            raise SmokeFailure(
                "iteration N+1 did not receive exactly iteration N context"
            )
        log(f"previous-iteration context passed (2 iterations, run {run_id})")

    def start_stream(
        self, conversation_id: str, workflow_id: str
    ) -> tuple[queue.Queue[Any], threading.Thread]:
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
    def wait_for_nth_event(
        output: queue.Queue[Any],
        events: list[dict[str, Any]],
        predicate: Any,
        count: int,
        timeout: float = 90,
    ) -> dict[str, Any]:
        seen = 0
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
                seen += 1
                if seen >= count:
                    return item
        raise SmokeFailure("timed out waiting for the expected loop iteration boundary")

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

    def run_cancellation_mid_loop(self) -> None:
        self.create_workflow("cancel-loop", self.cancel_loop_body("cancel-loop"))
        conversation_id = self.create_conversation()
        output, thread = self.start_stream(
            conversation_id, self.state.workflow_ids["cancel-loop"]
        )
        events: list[dict[str, Any]] = []
        # "start" publishes "running" before the barrier sleep, so catching
        # its 2nd running event guarantees the run is parked mid-iteration
        # (not racing alpha's near-instant fail-and-loop-back).
        second_start = self.wait_for_nth_event(
            output,
            events,
            lambda event: event["type"] == "workflow-node-status"
            and event["data"]["nodeKey"] == "start"
            and event["data"]["status"] == "running",
            count=2,
        )
        run_id = str(second_start["data"]["workflowRunId"])
        self.request(
            "business",
            "POST",
            f"/v1/workflow-runs/{run_id}/cancel",
            self.state.member_business_token,
            expected=(202,),
        )
        self.drain_stream(output, thread, events)
        self.assert_safe_events(events)
        run_statuses = [
            event["data"]["status"]
            for event in events
            if event["type"] == "workflow-run-status"
        ]
        if run_statuses[-1] != "cancelled":
            raise SmokeFailure(
                f"cancelled run did not emit its terminal status (trace: {run_statuses})"
            )

        def running_count(node_key: str) -> int:
            return sum(
                1
                for event in events
                if event["type"] == "workflow-node-status"
                and event["data"]["nodeKey"] == node_key
                and event["data"]["status"] == "running"
            )

        if running_count("start") != 2 or running_count("alpha") != 1:
            raise SmokeFailure(
                "cancellation did not land after exactly 2 iterations "
                f"(start running: {running_count('start')}, alpha running: {running_count('alpha')})"
            )
        _, response = self.request(
            "business",
            "GET",
            f"/v1/conversations/{conversation_id}",
            self.state.member_business_token,
        )
        if any(
            item["role"] == "assistant"
            for item in response["data"]["messages"]["data"]
        ):
            raise SmokeFailure("cancelled loop persisted an assistant message")
        log(f"cancellation mid-loop passed (stopped after 2 iterations, run {run_id})")

    def run(self) -> None:
        for phase, action in (
            ("token acquisition", self.acquire_tokens),
            ("iteration limit", self.run_iteration_limit_and_fallthrough),
            ("previous-iteration context", self.run_previous_iteration_context),
            ("cancellation", self.run_cancellation_mid_loop),
        ):
            try:
                action()
            except SmokeFailure:
                raise
            except Exception as exc:
                raise SmokeFailure(
                    f"{phase} failed ({type(exc).__name__})"
                ) from exc
        log("all workflow-loops scenarios passed; wrapper will verify cleanup")


def main() -> int:
    try:
        Acceptance(Config()).run()
    except Exception as exc:
        detail = str(exc) if isinstance(exc, SmokeFailure) else "operation failed"
        log(f"ERROR: {detail}; preserve recovery journal")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
