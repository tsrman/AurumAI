#!/usr/bin/env python3
"""Hermes Agent turns on the whole platform deployed by the chart on k3d.

Driven by ../smoke-test-k8s.sh with K8S_TEST_ACCEPTANCE=hermes, after the release is
up: backend, tool-worker, skill-executor and the Hermes namespace are the chart's own,
and the sandboxes are Pods. Two modes:

  deterministic  the release's own test gateway (no cost): the wiring end to end.
  real           a real model through the gateway the caller gave: a turn that computes,
                 installs a package through the guard and attaches a file.

The organization's Hermes settings are changed for the run and restored afterwards.
One PASS or FAIL line per check; the exit code is 1 if any failed.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

BACKEND = os.environ["K8S_HERMES_BACKEND_URL"]
ADMIN = os.environ["K8S_HERMES_ADMIN_URL"]
KEYCLOAK = os.environ["K8S_HERMES_KEYCLOAK_URL"]
USER, PASSWORD = os.environ["K8S_HERMES_USER"], os.environ["K8S_HERMES_PASSWORD"]
HN = os.environ["K8S_HERMES_NAMESPACE"]
RELEASE = os.environ["K8S_HERMES_RELEASE"]
PN = os.environ["K8S_HERMES_PLATFORM_NAMESPACE"]
IDLE_SECONDS = int(os.environ.get("K8S_HERMES_IDLE_SECONDS", "45"))
FAILED = 0
VERBOSE = os.environ.get("K8S_HERMES_VERBOSE") == "1"


def check(label: str, ok: bool, detail: str = "") -> None:
    global FAILED
    print(("PASS  " if ok else "FAIL  ") + label + (f"   [{detail[:400]}]" if detail and not ok else ""), flush=True)
    FAILED += 0 if ok else 1


def info(label: str, detail: Any) -> None:
    print(f"INFO  {label}: {detail}", flush=True)


def request(method: str, url: str, *, token: str | None = None, body: Any = None, timeout: float = 60) -> tuple[int, bytes]:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def api(method: str, url: str, token: str, body: Any = None) -> Any:
    status, raw = request(method, url, token=token, body=body)
    if status >= 300:
        raise RuntimeError(f"{method} {url} -> {status}: {raw[:300]!r}")
    return json.loads(raw)["data"] if raw else None


def token_for(client: str) -> str:
    form = urllib.parse.urlencode(
        {"grant_type": "password", "client_id": client, "username": USER, "password": PASSWORD}
    ).encode()
    req = urllib.request.Request(f"{KEYCLOAK}/realms/aurum-ai/protocol/openid-connect/token", data=form)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)["access_token"]


def kubectl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["kubectl", *args], text=True, capture_output=True)


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    kind, data = None, []
    for line in text.splitlines() + [""]:
        if line.startswith("event:"):
            kind = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data.append(line.split(":", 1)[1].strip())
        elif not line and kind is not None:
            try:
                events.append((kind, json.loads("\n".join(data))))
            except ValueError:
                events.append((kind, {"raw": "\n".join(data)}))
            kind, data = None, []
    return events


def turn(token: str, conversation: str, text: str, timeout: float = 900) -> tuple[int, list[tuple[str, dict[str, Any]]], str]:
    status, raw = request(
        "POST",
        f"{BACKEND}/v1/chat/stream",
        token=token,
        body={"conversation_id": conversation, "agent": "hermes", "model": "hermes", "messages": [{"role": "user", "content": text}]},
        timeout=timeout,
    )
    body = raw.decode(errors="replace")
    return status, parse_sse(body), body


def hermes_pods(conversation: str | None = None) -> list[str]:
    """The sandbox Pods, or those of one conversation (a run before this one may
    still have its own, waiting out its idle time)."""
    selector = "aurum-ai.component=hermes-sandbox"
    if conversation:
        selector += f",aurum-ai.hermes-conversation={conversation}"
    result = kubectl("-n", HN, "get", "pod", "-l", selector, "-o", "jsonpath={.items[*].metadata.name}")
    return result.stdout.split()


def pod_uid(name: str) -> str:
    return kubectl("-n", HN, "get", "pod", name, "-o", "jsonpath={.metadata.uid}").stdout


def wait_until(predicate, seconds: float, step: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


ADMIN_TOKEN = ""


def main(mode: str) -> None:
    global ADMIN_TOKEN
    business = token_for("business-ui")
    admin = ADMIN_TOKEN = token_for("admin-ui")
    original = api("GET", f"{ADMIN}/v1/admin/settings/hermes", admin)
    info("organization's Hermes settings before", original)
    model = "k8s-test" if mode == "deterministic" else os.environ["K8S_HERMES_REAL_MODEL"]
    try:
        api("PUT", f"{ADMIN}/v1/admin/settings/hermes", admin, {"model": model, "egressMode": "packages"})
        run(mode, business, admin, model)
    finally:
        restore = {"model": original.get("model")}
        if original.get("egressMode"):
            restore["egressMode"] = original["egressMode"]
        try:
            api("PUT", f"{ADMIN}/v1/admin/settings/hermes", admin, restore)
            info("organization's Hermes settings restored", restore)
        except Exception as exc:  # noqa: BLE001
            check("the organization's Hermes settings were restored", False, str(exc))


def final_message(token: str, conversation: str) -> dict[str, Any]:
    messages = api("GET", f"{BACKEND}/v1/conversations/{conversation}", token)["messages"]["data"]
    return [m for m in messages if m["role"] == "assistant"][-1]


def file_bytes(token: str, conversation: str, file_id: str) -> bytes:
    status, raw = request("GET", f"{BACKEND}/v1/conversations/{conversation}/files/{file_id}/content", token=token)
    return raw if status == 200 else b""


def tool_results(events: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, str]]:
    return [(d.get("tool", ""), (d.get("result") or {}).get("status", "")) for k, d in events if k == "tool-result"]


def answer(events: list[tuple[str, dict[str, Any]]]) -> str:
    return next((d.get("content", "") for k, d in events if k == "message-end"), "")


def executor_log(since: str = "30m") -> str:
    return kubectl("-n", PN, "logs", f"deploy/{RELEASE}-skill-executor", f"--since={since}").stdout


def run(mode: str, business: str, admin: str, model: str) -> None:
    conversation = api("POST", f"{BACKEND}/v1/conversations", business, {"title": f"Hermes on Kubernetes ({mode})", "model": model})["id"]
    info("conversation", conversation)
    if mode == "deterministic":
        deterministic(business, conversation)
    else:
        real(business, conversation)


def deterministic(token: str, conversation: str) -> None:
    t0 = time.monotonic()
    status, events, body = turn(token, conversation, "HERMES_FILES")
    info("turn", f"HTTP {status}, {len(events)} events, {time.monotonic() - t0:.0f}s")
    check("a Hermes turn streams to the end", status == 200 and any(k == "message-end" for k, _ in events), body[:300])
    results = tool_results(events)
    check("the terminal ran and the chart and the CSV were attached",
          results[:3] == [("hermes.terminal", "completed"), ("hermes.attach", "completed"), ("hermes.attach", "completed")], str(results))
    check("a platform secret in the workspace is refused when Hermes tries to attach it",
          results[3:] == [("hermes.attach", "failed")], str(results))
    message = final_message(token, conversation)
    check("the chart is an image of the answer", len(message.get("imageIds") or []) == 1, str(message.get("imageIds")))
    csv = next((f for f in message.get("files") or [] if f.get("name") == "e2e-data.csv"), None)
    check("the CSV is a file of the answer", csv is not None, str(message.get("files")))
    if csv is not None:
        check("the CSV downloads with exactly what Hermes wrote", file_bytes(token, conversation, csv["fileId"]) == b"month,revenue\nJan,10\nFeb,12\n")

    pods = hermes_pods(conversation)
    check("exactly one sandbox Pod runs for the conversation, in the Hermes namespace", len(pods) == 1, str(pods))
    if not pods:
        return
    pod = pods[0]
    uid = pod_uid(pod)
    spec = json.loads(kubectl("-n", HN, "get", "pod", pod, "-o", "json").stdout)
    container = spec["spec"]["containers"][0]
    check("the Pod runs unprivileged, read-only and without a service account token",
          spec["spec"]["automountServiceAccountToken"] is False
          and container["securityContext"]["readOnlyRootFilesystem"] is True
          and container["securityContext"]["capabilities"]["drop"] == ["ALL"]
          and spec["spec"]["securityContext"]["runAsNonRoot"] is True)
    check("the Pod has no DNS and names the guard by address",
          spec["spec"]["dnsPolicy"] == "None" and spec["spec"]["hostAliases"][0]["hostnames"] == ["hermes-egress"])
    # The token is a JWT: no "eyJ" and no bearer header anywhere in the Pod.
    check("the session token is in no Pod field", "eyJ" not in json.dumps(spec) and "Bearer" not in json.dumps(spec))

    # the platform's tools reach the sandbox over the MCP bridge, through the guard
    status, events, _ = turn(token, conversation, "HERMES_MCP:knowledge_search")
    text = answer(events)
    check("a platform tool is offered to Hermes and answers over the MCP bridge",
          status == 200 and text.startswith("HERMES_MCP_DONE:knowledge_search:"), text[:300])

    # memory is kept by the platform, not in the sandbox; the run never touches a
    # user's own memory, so it needs the account's to be empty
    memory_before = api("GET", f"{BACKEND}/v1/hermes/memory", token)
    memory_free = not (memory_before.get("memory") or memory_before.get("user"))
    if memory_free:
        status, events, _ = turn(token, conversation, "HERMES_MEMORY_ADD")
        check("Hermes adds an entry to its memory", status == 200 and answer(events).startswith("HERMES_MEMORY_ADDED"), answer(events)[:200])
        saved = api("GET", f"{BACKEND}/v1/hermes/memory", token)
        check("the platform kept the entry, and the user can read it", "AURUM_MEMORY_SENTINEL" in (saved.get("memory") or ""), str(saved)[:200])
    else:
        info("memory", "the account already has memory: its memory is left alone and not checked")

    # a skill Hermes writes becomes a draft for an administrator
    # A name of its own for each run: a draft an administrator decided is not drafted again.
    skill = f"k8s-acc-{uuid.uuid4().hex[:8]}"
    status, events, _ = turn(token, conversation, f"HERMES_SKILL_CREATE:{skill}")
    check("Hermes writes a skill", status == 200 and answer(events).startswith("HERMES_SKILL_CREATED"), answer(events)[:200])
    draft: dict[str, Any] | None = None

    def drafted() -> bool:
        nonlocal draft
        listed = api("GET", f"{ADMIN}/v1/admin/hermes/skill-drafts?status=pending", ADMIN_TOKEN)
        draft = next((d for d in listed if skill in json.dumps(d)), None)
        return draft is not None

    check("the skill appears as a pending draft for an administrator, not as a skill", wait_until(drafted, 90) and draft is not None, str(draft))
    if draft is not None:
        try:
            # A reviewer decides on the version they read: its checksum comes with the detail.
            detail = api("GET", f"{ADMIN}/v1/admin/hermes/skill-drafts/{draft['id']}", ADMIN_TOKEN)
            api("POST", f"{ADMIN}/v1/admin/hermes/skill-drafts/{draft['id']}/reject", ADMIN_TOKEN, {"bundleChecksum": detail["bundleChecksum"]})
            info("the draft was rejected, as a reviewer would", draft["id"])
        except Exception as exc:  # noqa: BLE001
            check("the pending draft could be rejected (it is left for the organization's administrator otherwise)", False, str(exc))

    # a second turn reuses the sandbox
    status, events, _ = turn(token, conversation, "HERMES_INSTRUCTIONS_SHOW")
    check("a second turn is answered", status == 200 and any(k == "message-end" for k, _ in events))
    check("the second turn reuses the same Pod", hermes_pods(conversation) == [pod] and pod_uid(pod) == uid, str(hermes_pods(conversation)))

    # idle: snapshotted and removed
    info("idle", f"waiting for the executor to retire the sandbox (idle TTL {IDLE_SECONDS}s)")
    gone = wait_until(lambda: not hermes_pods(conversation), IDLE_SECONDS + 120)
    check("an idle sandbox is retired and its Pod removed", gone, str(hermes_pods(conversation)))
    log = executor_log()
    check("the executor retired it (and stored a snapshot)", "Retiring Hermes sandbox" in log and "kept stopped" not in log, log[-400:])

    # the next turn restores the workspace into a new Pod
    status, events, _ = turn(token, conversation, "HERMES_INSTRUCTIONS_SHOW")
    check("a turn after the idle retirement is answered", status == 200 and any(k == "message-end" for k, _ in events))
    pods = hermes_pods(conversation)
    check("a new Pod was started for it", len(pods) == 1 and pod_uid(pods[0]) != uid, str(pods))
    if pods:
        shown = kubectl("-n", HN, "exec", pods[0], "--", "cat", "/workspace/out/e2e-data.csv")
        check("the workspace of the earlier Pod was restored into it from the snapshot", shown.stdout == "month,revenue\nJan,10\nFeb,12\n", shown.stdout + shown.stderr)

    # a new conversation is a new Hermes session on a new sandbox: it starts with the user's memory
    if memory_free:
        try:
            other = api("POST", f"{BACKEND}/v1/conversations", token, {"title": "Hermes on Kubernetes (memory)", "model": "k8s-test"})["id"]
            status, events, _ = turn(token, other, "HERMES_MEMORY_SHOW")
            check("a new conversation's sandbox starts with the memory the platform kept",
                  status == 200 and answer(events).startswith("HERMES_MEMORY_SEEN"), answer(events)[:200])
        finally:
            request("DELETE", f"{BACKEND}/v1/hermes/memory", token=token)
            cleared = api("GET", f"{BACKEND}/v1/hermes/memory", token)
            check("the user's memory is cleared again after the run", not cleared.get("memory"), str(cleared)[:200])


REAL_PROMPT = (
    "In your sandbox do this, using the terminal and Python: install the package `tabulate` with pip "
    "(into /workspace/lib with --target), then compute the sum of the first 50 prime numbers, write just "
    "that number to /workspace/out/primes.txt, attach that file, and finish by telling me the number."
)


def real(token: str, conversation: str) -> None:
    t0 = time.monotonic()
    status, events, body = turn(token, conversation, REAL_PROMPT, timeout=1200)
    info("turn", f"HTTP {status}, {len(events)} events, {time.monotonic() - t0:.0f}s, tools {tool_results(events)}")
    check("a Hermes turn on the real model streams to the end", status == 200 and any(k == "message-end" for k, _ in events), body[-400:])
    message = final_message(token, conversation)
    check("the answer states the sum of the first 50 primes (5117)", "5117" in message.get("content", ""), message.get("content", "")[:300])
    attached = next((f for f in message.get("files") or [] if f.get("name", "").endswith("primes.txt")), None)
    check("primes.txt is attached to the answer", attached is not None, str(message.get("files")))
    if attached is not None:
        check("it holds the number", file_bytes(token, conversation, attached["fileId"]).strip() == b"5117")
    guard = kubectl("-n", HN, "logs", f"deploy/{RELEASE}-hermes-egress", "--since=30m").stdout
    tunnels = [line for line in guard.splitlines() if '"outcome": "tunnel"' in line and '"phase": "open"' in line]
    hosts = {json.loads(line).get("host") for line in tunnels}
    check("pip reached the package registries through the guard (authorized by the real backend)",
          bool(hosts & {"pypi.org", "files.pythonhosted.org"}), f"tunnels to {sorted(h for h in hosts if h)}; refused: {[l[:160] for l in guard.splitlines() if 'refused' in l][:3]}")


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        check("the run did not crash", False, f"{type(exc).__name__}: {exc}")
    print(f"{FAILED} check(s) failed" if FAILED else "all checks passed", flush=True)
    sys.exit(1 if FAILED else 0)
