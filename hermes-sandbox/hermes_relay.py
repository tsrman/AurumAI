#!/usr/bin/env python3
"""The only interface skill-executor uses to talk to Hermes in its sandbox.

It runs through `docker exec`, so the sandbox opens no port to the platform.
It speaks to Hermes's API server on 127.0.0.1 and prints one JSON object per
line. Everything Hermes says is data for the caller to check, not trusted:
the agent can stop the server and answer in its place.

  health           exit 0 once the API server answers
  run FILE         FILE (placed by skill-executor, then removed) holds
                   {"input", "session_id", "key", "conversation_history"?};
                   starts the run once per key, then streams it
  events RUN_ID    replays and follows a run's events
  stop RUN_ID      asks Hermes to stop a run
"""

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

HERMES_HOME = os.environ.get("HERMES_HOME", "/workspace/.hermes")
STATE = os.environ.get("HERMES_RELAY_STATE", "/workspace/.aurum")
API = os.environ.get("HERMES_RELAY_API", "http://127.0.0.1:8642")
MAX_LINE = 256 * 1024
TERMINAL = {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}


def emit(kind: str, **fields: object) -> None:
    sys.stdout.write(json.dumps({"type": kind, **fields}, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def api_key() -> str:
    with open(os.path.join(HERMES_HOME, "config.yaml")) as config:
        return str(json.load(config)["platforms"]["api_server"]["extra"]["key"])


def request(method: str, path: str, body: object | None = None, timeout: float = 30):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}
    return urllib.request.urlopen(urllib.request.Request(API + path, data=data, headers=headers, method=method), timeout=timeout)


def write_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
        out.write(text)
    os.replace(temporary, path)


def follow(run_id: str) -> int:
    with request("GET", f"/v1/runs/{run_id}/events", timeout=None) as stream:
        for raw in stream:
            line = raw[:MAX_LINE].decode("utf-8", "replace").rstrip("\n")
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:])
            except ValueError:
                continue
            if isinstance(event, dict):
                emit("event", event=event)
                if event.get("event") in TERMINAL:
                    break
    with request("GET", f"/v1/runs/{run_id}") as response:
        status = json.load(response)
    emit("result", run_id=run_id, status=status.get("status"), output=status.get("output"), usage=status.get("usage"))
    return 0


def run(path: str) -> int:
    with open(path) as request_file:
        body = json.load(request_file)
    os.unlink(path)
    marker = os.path.join(STATE, "runs", hashlib.sha256(str(body["key"]).encode()).hexdigest())
    try:
        with open(marker) as existing:
            run_id = existing.read().strip()
    except FileNotFoundError:
        payload = {"input": body["input"], "session_id": body["session_id"]}
        if body.get("conversation_history"):
            payload["conversation_history"] = body["conversation_history"]
        with request("POST", "/v1/runs", payload) as response:
            run_id = str(json.load(response)["run_id"])
        # Hermes 0.19 does not replay Idempotency-Key; a retried activity
        # finds this marker and attaches to the run it already started.
        write_atomic(marker, run_id)
    emit("started", run_id=run_id)
    return follow(run_id)


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    if command == "health":
        try:
            with urllib.request.urlopen(API + "/health", timeout=3) as response:
                return 0 if response.status == 200 else 1
        except (OSError, urllib.error.URLError):
            return 1
    if command == "run" and len(argv) == 2:
        return run(argv[1])
    if command == "events" and len(argv) == 2:
        return follow(argv[1])
    if command == "stop" and len(argv) == 2:
        with request("POST", f"/v1/runs/{argv[1]}/stop", {}) as response:
            emit("stopping", status=json.load(response).get("status"))
        return 0
    print("usage: hermes-relay health|run FILE|events RUN|stop RUN", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except urllib.error.HTTPError as exc:
        emit("error", status=exc.code)
        sys.exit(1)
    except (OSError, ValueError, KeyError) as exc:
        emit("error", reason=type(exc).__name__)
        sys.exit(1)
