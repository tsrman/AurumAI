#!/usr/bin/env python3
"""Kubernetes exact-owned runner adapter for user-question acceptance."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import time
from typing import Any, Coroutine

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio.client.exceptions import ApiException

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from user_questions_e2e import (  # noqa: E402
    Config,
    FixtureController,
    FixtureError,
    build_parser,
    canonical,
    load_state,
    save_state,
)
from workflow_templates_ui_e2e import TEMPORAL_RPC  # noqa: E402


DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")
TEMPORAL_NAMESPACE = re.compile(r"[A-Za-z0-9_.-]{1,255}")
LOOPBACK_TEMPORAL = re.compile(r"(?:127\.0\.0\.1|localhost):([1-9][0-9]{0,4})")


class KubernetesTemporalTransport:
    """Read the k3d release's exact Temporal namespace through loopback."""

    def __init__(self, host: str, namespace: str) -> None:
        match = LOOPBACK_TEMPORAL.fullmatch(host)
        if (
            match is None
            or int(match.group(1)) > 65535
            or TEMPORAL_NAMESPACE.fullmatch(namespace) is None
        ):
            raise FixtureError("invalid Kubernetes Temporal scope")
        self.host = host
        self.namespace = namespace

    def request(self, action: str, workflow_id: str, run_id: str | None) -> Any:
        if action not in {"read", "terminate"}:
            raise FixtureError("invalid Temporal operation")
        environment = {
            **os.environ,
            "APP_TEMPORAL_HOST": self.host,
            "APP_TEMPORAL_NAMESPACE": self.namespace,
        }
        result = subprocess.run(
            [sys.executable, "-c", TEMPORAL_RPC],
            cwd=SCRIPT_DIR.parent / "business-backend",
            env=environment,
            input=json.dumps(
                {"action": action, "workflow_id": workflow_id, "run_id": run_id}
            ),
            capture_output=True,
            text=True,
            timeout=55,
        )
        if result.returncode:
            raise FixtureError("Temporal RPC unavailable; journal retained")
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise FixtureError("Temporal RPC returned invalid metadata") from None

    def read(self, workflow_id: str, run_id: str | None = None) -> Any:
        return self.request("read", workflow_id, run_id)

    def terminate(self, workflow_id: str, run_id: str) -> Any:
        return self.request("terminate", workflow_id, canonical(run_id))


class KubernetesPodControl:
    """Pin and remove a single runner Pod with an API-level UID precondition."""

    def __init__(
        self,
        *,
        namespace: str,
        release: str,
        state_path: Path,
        kubeconfig: str | None = None,
        core_v1: Any = None,
        api_error: type[Exception] = ApiException,
        wait_seconds: float = 90.0,
    ) -> None:
        if not DNS_LABEL.fullmatch(namespace) or not DNS_LABEL.fullmatch(release):
            raise FixtureError("invalid Kubernetes ownership scope")
        self.namespace = namespace
        self.release = release
        self.state_path = state_path
        self.kubeconfig = kubeconfig
        self._core = core_v1
        self._injected_core = core_v1 is not None
        self._api_client: Any = None
        self.api_error = api_error
        self.wait_seconds = wait_seconds

    @staticmethod
    def _run(awaitable: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run(awaitable)

    async def _client(self) -> Any:
        if self._core is not None:
            return self._core
        await k8s_config.load_kube_config(config_file=self.kubeconfig)
        self._api_client = k8s_client.ApiClient()
        self._core = k8s_client.CoreV1Api(self._api_client)
        return self._core

    async def _close(self) -> None:
        if self._api_client is not None:
            await self._api_client.close()
            self._api_client = None
        if not self._injected_core:
            self._core = None

    async def _managed(self, awaitable: Coroutine[Any, Any, Any]) -> Any:
        try:
            return await awaitable
        finally:
            await self._close()

    async def _read_async(self, conversation_id: str) -> Any | None:
        core = await self._client()
        try:
            return await core.read_namespaced_pod(
                name=f"runner-{conversation_id}", namespace=self.namespace
            )
        except self.api_error as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise FixtureError("Kubernetes runner read failed") from None

    def _expected_labels(self, conversation_id: str) -> dict[str, str]:
        return {
            "app.kubernetes.io/instance": self.release,
            "aurum-ai.component": "conversation-runner",
            "aurum-ai.conversation": conversation_id,
        }

    def _resource(self, pod: Any, conversation_id: str) -> dict[str, str]:
        metadata = getattr(pod, "metadata", None)
        actual = {
            "kind": "kubernetes",
            "name": getattr(metadata, "name", None),
            "namespace": getattr(metadata, "namespace", None),
            "release": self.release,
            "uid": getattr(metadata, "uid", None),
        }
        if (
            actual["name"] != f"runner-{conversation_id}"
            or actual["namespace"] != self.namespace
            or getattr(metadata, "labels", None) != self._expected_labels(conversation_id)
        ):
            raise FixtureError("Kubernetes runner ownership mismatch")
        try:
            canonical(actual["uid"])
        except FixtureError:
            raise FixtureError("Kubernetes runner ownership mismatch") from None
        return actual  # type: ignore[return-value]

    async def _pin_owned_async(self, conversation_id: str) -> None:
        state = load_state(self.state_path)
        if conversation_id not in state.conversation_ids:
            raise FixtureError("Kubernetes runner conversation is not journal-owned")
        deadline = time.monotonic() + self.wait_seconds
        while True:
            pod = await self._read_async(conversation_id)
            if pod is not None:
                resource = self._resource(pod, conversation_id)
                previous = state.runner_resources.get(conversation_id)
                if previous is not None and previous != resource:
                    raise FixtureError("Kubernetes runner ownership mismatch")
                state.runner_resources[conversation_id] = resource
                save_state(self.state_path, state)
                return
            if time.monotonic() >= deadline:
                raise FixtureError("Kubernetes runner was not created")
            await asyncio.sleep(0.5)

    def pin_owned(self, conversation_id: str) -> None:
        self._run(self._managed(self._pin_owned_async(canonical(conversation_id))))

    def _pinned(self, conversation_id: str) -> dict[str, str] | None:
        state = load_state(self.state_path)
        return state.runner_resources.get(conversation_id)

    async def _delete_async(self, conversation_id: str, uid: str) -> None:
        core = await self._client()
        body = k8s_client.V1DeleteOptions(
            grace_period_seconds=0,
            preconditions=k8s_client.V1Preconditions(uid=uid),
        )
        try:
            await core.delete_namespaced_pod(
                name=f"runner-{conversation_id}",
                namespace=self.namespace,
                body=body,
            )
        except self.api_error as exc:
            if getattr(exc, "status", None) != 404:
                raise FixtureError("Kubernetes runner UID-precondition delete failed") from None

    async def _remove_owned_async(self, conversation_id: str) -> None:
        pod = await self._read_async(conversation_id)
        if pod is None:
            return
        pinned = self._pinned(conversation_id)
        if pinned is None:
            raise FixtureError("Kubernetes runner is not pinned in the private journal")
        current = self._resource(pod, conversation_id)
        if current != pinned:
            raise FixtureError("Kubernetes runner ownership mismatch")
        await self._delete_async(conversation_id, pinned["uid"])
        deadline = time.monotonic() + self.wait_seconds
        while True:
            remaining = await self._read_async(conversation_id)
            if remaining is None:
                return
            if self._resource(remaining, conversation_id) != pinned:
                raise FixtureError("Kubernetes runner ownership mismatch")
            if time.monotonic() >= deadline:
                raise FixtureError("Kubernetes runner deletion timed out")
            await asyncio.sleep(0.5)

    def remove_owned(self, conversation_id: str) -> None:
        self._run(self._managed(self._remove_owned_async(canonical(conversation_id))))

    async def _assert_absent_async(self, conversation_id: str) -> None:
        if await self._read_async(conversation_id) is not None:
            raise FixtureError("Kubernetes runner remains after cleanup")

    def assert_absent(self, conversation_id: str) -> None:
        self._run(self._managed(self._assert_absent_async(canonical(conversation_id))))

    def stop_owned(self, conversation_id: str) -> None:
        del conversation_id
        raise FixtureError("Kubernetes worker restart injection is Compose-only")

    def start_owned(self, conversation_id: str) -> None:
        del conversation_id
        raise FixtureError("Kubernetes worker restart injection is Compose-only")


def parser() -> argparse.ArgumentParser:
    return build_parser()


def main() -> int:
    args = parser().parse_args()
    try:
        namespace = os.environ["K8S_QUESTION_RUNNER_NAMESPACE"]
        release = os.environ["K8S_QUESTION_RELEASE_NAME"]
        control = KubernetesPodControl(
            namespace=namespace,
            release=release,
            state_path=args.state,
            kubeconfig=os.getenv("KUBECONFIG"),
        )
        temporal = KubernetesTemporalTransport(
            os.getenv("K8S_QUESTION_TEMPORAL_HOST", "127.0.0.1:7233"),
            os.environ["K8S_QUESTION_TEMPORAL_NAMESPACE"],
        )
        controller = FixtureController(
            Config(),
            args.state,
            args.marker,
            runner_cleaner=control,
            temporal=temporal,
        )
        if args.command == "record":
            controller.record(args.kind, args.id)
            if args.kind == "conversation":
                control.pin_owned(args.id)
        elif args.command == "defer-delivery":
            controller.defer_delivery(args.question, args.seconds)
        elif args.command in {"stop-worker", "start-worker"}:
            getattr(controller, args.command.replace("-", "_"))(args.conversation)
        else:
            getattr(controller, args.command.replace("-", "_"))()
    except Exception as exc:  # noqa: BLE001 - operator-safe boundary
        detail = str(exc) if isinstance(exc, FixtureError) else "operation failed"
        print(
            f"[user-questions-k8s-e2e] {detail}; preserve recovery journal",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
