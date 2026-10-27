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
  clarify RUN FILE answers a run's pending question; FILE (placed by
                   skill-executor, then removed) holds {"request_id",
                   "execution_generation", "answer"}
  active           the recent runs Hermes still reports as queued or running
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERMES_HOME = os.environ.get("HERMES_HOME", "/workspace/.hermes")
STATE = os.environ.get("HERMES_RELAY_STATE", "/workspace/.aurum")
API = os.environ.get("HERMES_RELAY_API", "http://127.0.0.1:8642")
MAX_LINE = 256 * 1024
TERMINAL = {"run.completed", "run.failed", "run.cancelled", "run.interrupted"}
ACTIVE = {"queued", "running", "waiting_for_approval"}
# A run not yet settled: still active, or asked to stop.
UNSETTLED = ACTIVE | {"stopping"}
POLL_SECONDS = float(os.environ.get("HERMES_RELAY_POLL_SECONDS", "2"))
# Only runs started within the longest turn deadline, newest first, are asked about.
RECENT_SECONDS = 86400
RECENT_RUNS = 32


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


def run_status(run_id: str) -> dict:
    with request("GET", f"/v1/runs/{run_id}") as response:
        return json.load(response)


def stream_events(run_id: str) -> None:
    """Hermes hands a run's events to one subscriber, once, and drops the queue
    when that subscriber leaves: a reattach finds no events (404), or a queue
    an earlier subscriber still reads, so only keepalives arrive. Either way
    the run's status decides when it is over."""
    try:
        stream = request("GET", f"/v1/runs/{run_id}/events", timeout=None)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return
        raise
    with stream:
        for raw in stream:
            line = raw[:MAX_LINE].decode("utf-8", "replace").rstrip("\n")
            if line == ": keepalive" and run_status(run_id).get("status") not in UNSETTLED:
                return
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:])
            except ValueError:
                continue
            if isinstance(event, dict):
                emit("event", event=event)
                if event.get("event") in TERMINAL:
                    return


def follow(run_id: str) -> int:
    stream_events(run_id)
    status = run_status(run_id)
    while status.get("status") in UNSETTLED:
        time.sleep(POLL_SECONDS)
        status = run_status(run_id)
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


def clarify(run_id: str, path: str) -> int:
    with open(path) as answer_file:
        body = json.load(answer_file)
    os.unlink(path)
    try:
        with request("POST", f"/v1/runs/{run_id}/clarify", body) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        # 409: the question was settled meanwhile (answered, timed out, run ended).
        status = exc.code
    emit("clarify", status=status)
    return 0 if status < 300 else 3


def active() -> int:
    runs = os.path.join(STATE, "runs")
    try:
        markers = sorted(os.scandir(runs), key=lambda entry: entry.stat().st_mtime, reverse=True)
    except FileNotFoundError:
        markers = []
    found = []
    for entry in markers[:RECENT_RUNS]:
        if time.time() - entry.stat().st_mtime > RECENT_SECONDS:
            break
        with open(entry.path) as marker:
            run_id = marker.read().strip()
        try:
            with request("GET", f"/v1/runs/{run_id}", timeout=10) as response:
                status = json.load(response).get("status")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        if status in ACTIVE:
            found.append(run_id)
    emit("active", run_ids=found)
    return 0


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
    if command == "clarify" and len(argv) == 3:
        return clarify(argv[1], argv[2])
    if command == "active" and len(argv) == 1:
        return active()
    if command == "stop" and len(argv) == 2:
        with request("POST", f"/v1/runs/{argv[1]}/stop", {}) as response:
            emit("stopping", status=json.load(response).get("status"))
        return 0
    print("usage: hermes-relay health|run FILE|events RUN|stop RUN|clarify RUN FILE|active", file=sys.stderr)
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
