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
import hashlib
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from typing import Any

BACKEND = os.environ["K8S_HERMES_BACKEND_URL"]
ADMIN = os.environ["K8S_HERMES_ADMIN_URL"]
KEYCLOAK = os.environ["K8S_HERMES_KEYCLOAK_URL"]
USER, PASSWORD = os.environ["K8S_HERMES_USER"], os.environ["K8S_HERMES_PASSWORD"]
HN = os.environ["K8S_HERMES_NAMESPACE"]
RELEASE = os.environ["K8S_HERMES_RELEASE"]
PN = os.environ["K8S_HERMES_PLATFORM_NAMESPACE"]
IDLE_SECONDS = int(os.environ.get("K8S_HERMES_IDLE_SECONDS", "45"))
# Previews: the host port the cluster's HTTPS entry point is published on, and the
# domain of the preview origins (a name no resolver knows: curl and the sockets
# below are told where it is).
PREVIEW_HTTPS_PORT = os.environ.get("K8S_HERMES_PREVIEW_HTTPS_PORT", "")
PREVIEW_DOMAIN = os.environ.get("K8S_HERMES_PREVIEW_DOMAIN", "preview.example.test")
PREVIEW_PORT = 8898
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


def make_xlsx(rows: list[list[Any]]) -> bytes:
    """A minimal workbook (inline strings, no styles): the standard library only."""

    def cell(ref: str, value: Any) -> str:
        if isinstance(value, str):
            return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'
        return f'<c r="{ref}"><v>{value}</v></c>'

    sheet_rows = "".join(
        f'<row r="{r}">' + "".join(cell(f"{'ABCDEFGH'[c]}{r}", v) for c, v in enumerate(row)) + "</row>"
        for r, row in enumerate(rows, start=1)
    )
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
        ),
        "xl/workbook.xml": (
            f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{main}" xmlns:r="{rel}">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
        ),
        "xl/worksheets/sheet1.xml": (
            f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{main}"><sheetData>{sheet_rows}</sheetData></worksheet>'
        ),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def upload(token: str, conversation: str, name: str, data: bytes) -> dict[str, Any]:
    boundary = uuid.uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{BACKEND}/v1/conversations/{conversation}/files",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)["data"]


def turn(
    token: str, conversation: str, text: str, timeout: float = 900, file_ids: list[str] | None = None
) -> tuple[int, list[tuple[str, dict[str, Any]]], str]:
    payload: dict[str, Any] = {
        "conversation_id": conversation,
        "agent": "hermes",
        "model": "hermes",
        "messages": [{"role": "user", "content": text}],
    }
    if file_ids:
        # The files belong to the message that carries them.
        payload["messages"][0]["fileIds"] = file_ids
    status, raw = request("POST", f"{BACKEND}/v1/chat/stream", token=token, body=payload, timeout=timeout)
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
    answers = [m for m in messages if m["role"] == "assistant"]
    return answers[-1] if answers else {}


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

    uploads(token)
    previews(token, ADMIN_TOKEN)


def uploads(token: str) -> None:
    """Files the user gives Hermes reach its workspace, and what it makes from them comes back."""
    conversation = api("POST", f"{BACKEND}/v1/conversations", token, {"title": "Hermes on Kubernetes (uploads)", "model": "k8s-test"})["id"]
    january = make_xlsx([["region", "month", "sales"], ["North", "Jan", 100], ["South", "Jan", 50]])
    february = make_xlsx([["region", "month", "sales"], ["North", "Feb", 120], ["South", "Feb", 70]])
    sent = [upload(token, conversation, "january.xlsx", january), upload(token, conversation, "february.xlsx", february)]
    check("two workbooks are accepted as uploads, typed by their bytes",
          len(sent) == 2 and all("spreadsheet" in (f.get("contentType") or "") for f in sent), str(sent))
    status, events, body = turn(token, conversation, "HERMES_INPUTS", file_ids=[f["fileId"] for f in sent])
    text = answer(events)
    check("a turn on the uploads streams to the end", status == 200 and any(k == "message-end" for k, _ in events), body[-300:])
    check("Hermes read both workbooks in its workspace and pivoted them (North 100+120, South 50+70)",
          "North=220" in text and "South=120" in text, text[:400])
    pod = hermes_pods(conversation)
    if pod:
        listed = kubectl("-n", HN, "exec", pod[0], "--", "ls", "/workspace/inputs").stdout.split()
        check("the uploads are in the workspace's inputs folder",
              {"january.xlsx", "february.xlsx"} <= set(listed), str(listed))
    message = final_message(token, conversation)
    names = {f.get("name") for f in message.get("files") or []}
    check("the pivot workbook and the deck are attached to the answer", {"pivot.xlsx", "report.pptx"} <= names, str(names))
    pivot = next((f for f in message.get("files") or [] if f.get("name") == "pivot.xlsx"), None)
    if pivot is not None:
        content = file_bytes(token, conversation, pivot["fileId"])
        check("the pivot workbook downloads as a real workbook", content[:2] == b"PK" and b"xl/workbook.xml" in content, str(content[:40]))


# An app for the sandbox, standard library only: a page that reports the Cookie header it
# was sent (the preview service must not pass its own), and a WebSocket that echoes.
PREVIEW_APP = r'''
import base64, hashlib, socket, threading
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

def head(c):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = c.recv(4096)
        if not chunk:
            return None
        data += chunk
    return data

def exactly(c, n):
    data = b""
    while len(data) < n:
        chunk = c.recv(n - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data

def handle(c):
    try:
        raw = head(c)
        if raw is None:
            return
        lines = raw.split(b"\r\n\r\n")[0].split(b"\r\n")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            headers[name.strip().lower().decode()] = value.strip().decode()
        if headers.get("upgrade", "").lower() == "websocket":
            accept = base64.b64encode(hashlib.sha1(headers["sec-websocket-key"].encode() + GUID).digest())
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            while True:
                first, second = exactly(c, 2)
                size = second & 0x7F
                if size == 126:
                    size = int.from_bytes(exactly(c, 2), "big")
                mask = exactly(c, 4) if second & 0x80 else b"\0\0\0\0"
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(exactly(c, size)))
                if first & 0x0F == 8:
                    return
                reply = b"echo:" + payload
                c.sendall(bytes([0x81, len(reply)]) + reply)
        else:
            body = b"preview-ok;cookie=" + headers.get("cookie", "").encode() + b";path=" + lines[0].split(b" ")[1]
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
    except (OSError, EOFError):
        pass
    finally:
        c.close()

server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", %PORT%))
server.listen(16)
while True:
    conn, _ = server.accept()
    threading.Thread(target=handle, args=(conn,), daemon=True).start()
'''.replace("%PORT%", str(PREVIEW_PORT))


def curl(url: str, jar: str, *extra: str) -> tuple[int, str, str]:
    """GET through the cluster's HTTPS entry point: the preview origin resolves to loopback and
    the certificate is the run's own, self-signed. Returns the status, every response's headers
    (a redirect has two) and the last body."""
    parts = urllib.parse.urlsplit(url)
    headers_file, body_file = jar + ".headers", jar + ".body"
    result = subprocess.run(
        ["curl", "-sk", "--max-time", "30", "-D", headers_file, "-o", body_file, "-w", "%{http_code}", "-b", jar, "-c", jar,
         "--resolve", f"{parts.hostname}:{parts.port or 443}:127.0.0.1", *extra, url],
        text=True,
        capture_output=True,
    )
    code = int(result.stdout.strip() or 0)
    with open(headers_file, errors="replace") as handle:
        headers = handle.read()
    with open(body_file, errors="replace") as handle:
        body = handle.read()
    return code, headers, body


def websocket(host: str, cookie: str) -> ssl.SSLSocket:
    """Open a WebSocket on the preview through the HTTPS entry point and prove it echoes."""
    raw = socket.create_connection(("127.0.0.1", int(PREVIEW_HTTPS_PORT)), 10)
    context = ssl.create_default_context()
    context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    tls = context.wrap_socket(raw, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    tls.sendall(
        (
            f"GET /socket HTTP/1.1\r\nHost: {host}:{PREVIEW_HTTPS_PORT}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: https://{host}:{PREVIEW_HTTPS_PORT}\r\n"
            f"Cookie: {cookie}\r\n\r\n"
        ).encode()
    )
    reply = b""
    while b"\r\n\r\n" not in reply:
        chunk = tls.recv(4096)
        if not chunk:
            break
        reply += chunk
    if b" 101 " not in reply.split(b"\r\n")[0]:
        raise RuntimeError(reply[:200].decode(errors="replace"))
    return tls


def ws_exchange(tls: ssl.SSLSocket, text: bytes) -> bytes:
    mask = os.urandom(4)
    tls.sendall(bytes([0x81, 0x80 | len(text)]) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(text)))
    header = tls.recv(2)
    return tls.recv(header[1] & 0x7F) if len(header) == 2 else b""


def set_capabilities(admin: str, capabilities: list[str]) -> None:
    api("PUT", f"{ADMIN}/v1/admin/authorization/capabilities/organization", admin, {"capabilities": capabilities})


def previews(token: str, admin: str) -> None:
    """A web app in the sandbox opened from a browser's side through the Ingress over TLS: the
    one-time ticket, the host-only cookie, what the app is not told, a WebSocket, and revocation."""
    if not PREVIEW_HTTPS_PORT:
        info("previews", "no HTTPS port for previews was given (K8S_HERMES_PREVIEW_HTTPS_PORT): not checked")
        return
    conversation = api("POST", f"{BACKEND}/v1/conversations", token, {"title": "Hermes on Kubernetes (previews)", "model": "k8s-test"})["id"]
    # Prepare a sandbox for the conversation, start the app in it, then let Hermes share the port.
    status, events, _ = turn(token, conversation, "HERMES_INSTRUCTIONS_SHOW")
    pods = hermes_pods(conversation)
    check("a sandbox is running for the preview conversation", status == 200 and len(pods) == 1, str(pods))
    if len(pods) != 1:
        return
    pod = pods[0]
    written = subprocess.run(["kubectl", "-n", HN, "exec", "-i", pod, "--", "sh", "-c", "cat > /tmp/preview_app.py"],
                             input=PREVIEW_APP, text=True, capture_output=True)
    kubectl("-n", HN, "exec", pod, "--", "sh", "-c", "nohup python3 /tmp/preview_app.py >/tmp/preview_app.log 2>&1 &")
    listening = wait_until(
        lambda: kubectl("-n", HN, "exec", pod, "--", "python3", "-c",
                        f"import socket; socket.create_connection(('127.0.0.1', {PREVIEW_PORT}), 2)").returncode == 0, 30, 1.0)
    check("the web app listens on loopback in the sandbox", written.returncode == 0 and listening, written.stderr[-200:])
    status, events, _ = turn(token, conversation, "HERMES_PREVIEW")
    message = final_message(token, conversation)
    shared = message.get("previews") or []
    check("Hermes shares the app as a preview of its answer", status == 200 and len(shared) == 1 and shared[0].get("port") == PREVIEW_PORT, str(message.get("previews")) + answer(events)[:200])
    if len(shared) != 1:
        return

    status, raw = request("POST", f"{BACKEND}/v1/conversations/{conversation}/previews/{shared[0]['previewId']}/ticket", token=token)
    url = json.loads(raw).get("url", "") if status == 200 else ""
    parts = urllib.parse.urlsplit(url)
    check("the owner gets a one-time URL on an origin of the preview's own, over HTTPS",
          parts.scheme == "https" and (parts.hostname or "").endswith("." + PREVIEW_DOMAIN) and "ticket=" in parts.query, url[:120])
    if not parts.hostname:
        return
    host = parts.hostname
    jar = os.path.join(os.environ.get("K8S_HERMES_WORK_DIR", "/tmp"), f"preview-{uuid.uuid4().hex[:8]}.jar")

    code, headers, body = curl(url, jar, "-L")
    check("the ticket opens the preview and the app answers through traefik and the preview service",
          code == 200 and body.startswith("preview-ok"), f"{code} {body[:120]}")
    # traefik answers over HTTP/2: lowercase names, and no CR in what curl writes.
    cookie_line = next((line for line in headers.splitlines() if line.lower().startswith("set-cookie:") and "aurum-preview" in line), "")
    check("the grant is a host-only __Host- cookie: Secure, HttpOnly, SameSite=Lax, Path=/, no Domain",
          cookie_line.split(":", 1)[-1].strip().startswith("__Host-aurum-preview=")
          and all(flag in cookie_line for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/")) and "Domain=" not in cookie_line,
          cookie_line)
    check("the app is not sent the preview's cookie (nor any credential of the platform)",
          "aurum-preview" not in body and "Bearer" not in body and "cookie=;" in body, body[:200])
    code, _, body = curl(f"https://{parts.netloc}/another/page", jar)
    check("a later request on the same origin is served from the cookie", code == 200 and "path=/another/page" in body, f"{code} {body[:100]}")
    code, _, _ = curl(url, os.path.join(os.path.dirname(jar), f"fresh-{uuid.uuid4().hex[:8]}.jar"))
    check("a ticket works once", code != 200, str(code))
    code, _, _ = curl(f"https://{parts.netloc}/", os.path.join(os.path.dirname(jar), f"empty-{uuid.uuid4().hex[:8]}.jar"))
    check("without the cookie the origin shows nothing of the app", code != 200, str(code))

    cookie_value = next((line.split("\t")[-2] + "=" + line.split("\t")[-1] for line in open(jar).read().splitlines() if "aurum-preview" in line), "")
    original = api("GET", f"{ADMIN}/v1/admin/authorization/capabilities/organization", admin)["capabilities"]
    try:
        tls = websocket(host, cookie_value)
        check("a WebSocket opens through the Ingress and the preview service and echoes", ws_exchange(tls, b"ping") == b"echo:ping")
        set_capabilities(admin, [c for c in original if c != "agents.hermes"])
        info("revocation", "agents.hermes taken from the organization; waiting for the preview service's re-check")
        tls.settimeout(60)
        ended = False
        try:
            tls.recv(1)  # an echo server sends nothing unprompted: this returns when the connection ends
            ended = True
        except TimeoutError:
            ended = False
        except (OSError, ssl.SSLError):
            ended = True
        check("revoking access ends the open WebSocket within the re-check", ended)
        code, _, _ = curl(f"https://{parts.netloc}/after-revocation", jar)
        check("and a new request with the same cookie is refused", code != 200, str(code))
    finally:
        set_capabilities(admin, original)
        restored = api("GET", f"{ADMIN}/v1/admin/authorization/capabilities/organization", admin)["capabilities"]
        check("the organization's capabilities are restored", sorted(restored) == sorted(original), str(restored))


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
