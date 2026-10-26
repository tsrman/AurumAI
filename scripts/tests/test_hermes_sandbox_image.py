"""The hermes-sandbox image and its relay."""

from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
IMAGE = ROOT / "hermes-sandbox"


def test_every_dependency_is_pinned_by_hash() -> None:
    requirements = (IMAGE / "requirements.txt").read_text()
    pins = re.findall(r"^([A-Za-z0-9_.-]+(?:\[[^\]]+\])?)==([^ \\\n]+)", requirements, re.M)
    blocks = [block for block in re.split(r"\n(?=[A-Za-z0-9_.-]+==)", requirements) if "==" in block]

    assert ("mcp", "2.3.0") in pins and ("aiohttp", "3.14.5") in pins
    assert len(pins) == len(blocks) > 50
    assert all("--hash=sha256:" in block for block in blocks)
    assert "git+" not in requirements
    assert not re.search(r"^hermes-agent\b", requirements, re.M)


def test_image_installs_by_hash_and_runs_unprivileged_without_compilers() -> None:
    dockerfile = (IMAGE / "Dockerfile").read_text()

    assert "FROM python:3.14-slim" in dockerfile
    assert "pip install --no-cache-dir --require-hashes --no-deps --only-binary=:all: -r" in dockerfile
    # Hermes's own source tree, from its tag archive, verified by hash.
    assert re.search(r"^ARG HERMES_SHA256=[0-9a-f]{64}$", dockerfile, re.M)
    assert 'sha256sum -c -' in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert not re.search(r"build-essential|\bgcc\b|\bg\+\+|\bmake\b", dockerfile)
    assert 'ENTRYPOINT ["/usr/local/bin/hermes-sandbox-start"]' in dockerfile


def test_start_script_keeps_secrets_out_of_the_container_environment() -> None:
    start = (IMAGE / "start.sh").read_text()

    assert 'NO_PROXY="hermes-egress,localhost,127.0.0.1"' in start
    assert "HTTP_PROXY=" not in start
    assert "exec hermes gateway run" in start


def test_image_is_built_and_published_with_the_others() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    publish = (ROOT / ".github" / "workflows" / "publish-images.yml").read_text()
    manifest = json.loads((ROOT / "release" / "oss-manifest.json").read_text())

    assert " hermes-sandbox " in ci
    assert "{ name: aurum-ai-hermes-sandbox, context: hermes-sandbox }" in publish
    assert "hermes-sandbox/**" in manifest["include"]


class FakeHermes(http.server.BaseHTTPRequestHandler):
    runs: list[dict] = []
    statuses: dict[str, str] = {}
    # "gone": the run's event queue was consumed by an earlier subscriber;
    # "stalled": another subscriber holds it, so only keepalives arrive.
    events_mode = "normal"
    polls: list[str] = []

    def log_message(self, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.headers.get("Authorization") != "Bearer relay-key":
            return self._send(401, b"{}")
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeHermes.runs.append(body)
        self._send(202, json.dumps({"run_id": f"run_{len(FakeHermes.runs)}", "status": "started"}).encode())

    def do_GET(self) -> None:
        if self.path.endswith("/events"):
            if FakeHermes.events_mode == "gone":
                return self._send(404, b"{}")
            if FakeHermes.events_mode == "stalled":
                return self._send(200, b": keepalive\n\n: keepalive\n\n", "text/event-stream")
            events = [
                ": opened",
                'data: {"event": "tool.started", "tool": "terminal"}',
                "data: not json",
                'data: {"event": "run.completed", "output": "done"}',
                'data: {"event": "after the end"}',
            ]
            return self._send(200, ("\n".join(events) + "\n").encode(), "text/event-stream")
        run_id = self.path.rsplit("/", 1)[-1]
        status = FakeHermes.polls.pop(0) if FakeHermes.polls else FakeHermes.statuses.get(run_id, "completed")
        if status == "missing":
            return self._send(404, b"{}")
        self._send(200, json.dumps({"status": status, "output": "done", "usage": {"total_tokens": 3}}).encode())


@pytest.fixture
def relay(tmp_path: Path):  # noqa: ANN201
    FakeHermes.runs, FakeHermes.statuses, FakeHermes.events_mode, FakeHermes.polls = [], {}, "normal", []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHermes)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps({"platforms": {"api_server": {"extra": {"key": "relay-key"}}}}))
    env = {
        **os.environ,
        "HERMES_HOME": str(home),
        "HERMES_RELAY_STATE": str(tmp_path / ".aurum"),
        "HERMES_RELAY_API": f"http://127.0.0.1:{server.server_address[1]}",
        "HERMES_RELAY_POLL_SECONDS": "0.05",
    }

    def run(*args: str) -> list[dict]:
        result = subprocess.run(
            [sys.executable, str(IMAGE / "hermes_relay.py"), *args], env=env, text=True, capture_output=True, timeout=30
        )
        return [json.loads(line) for line in result.stdout.splitlines()]

    yield run, tmp_path
    server.shutdown()


def _request(tmp_path: Path, key: str) -> str:
    path = tmp_path / f"request-{key}.json"
    path.write_text(json.dumps({"key": key, "input": "hi", "session_id": "conv"}))
    return str(path)


def test_relay_streams_events_and_the_final_status(relay) -> None:
    run, tmp_path = relay

    lines = run("run", _request(tmp_path, "turn-1"))

    assert [line["type"] for line in lines] == ["started", "event", "event", "result"]
    assert lines[-1] == {"type": "result", "run_id": "run_1", "status": "completed", "output": "done", "usage": {"total_tokens": 3}}
    assert FakeHermes.runs == [{"input": "hi", "session_id": "conv"}]


def test_a_retried_run_attaches_instead_of_starting_again(relay) -> None:
    run, tmp_path = relay

    run("run", _request(tmp_path, "turn-1"))
    again = run("run", _request(tmp_path, "turn-1"))
    other = run("run", _request(tmp_path, "turn-2"))

    assert again[0] == {"type": "started", "run_id": "run_1"}
    assert len(FakeHermes.runs) == 2
    assert other[-1]["status"] == "completed"
    assert not (tmp_path / "request-turn-1.json").exists()


def test_a_reattach_after_the_events_were_consumed_polls_for_the_result(relay) -> None:
    run, tmp_path = relay
    run("run", _request(tmp_path, "turn-1"))
    FakeHermes.events_mode = "gone"
    FakeHermes.polls = ["running", "running", "completed"]

    lines = run("run", _request(tmp_path, "turn-1"))

    assert [line["type"] for line in lines] == ["started", "result"]
    assert lines[-1]["status"] == "completed" and lines[-1]["output"] == "done"
    assert len(FakeHermes.runs) == 1


def test_a_stream_held_by_another_subscriber_ends_once_the_run_has(relay) -> None:
    run, tmp_path = relay
    FakeHermes.events_mode = "stalled"
    FakeHermes.polls = ["running", "completed"]

    lines = run("run", _request(tmp_path, "turn-1"))

    assert [line["type"] for line in lines] == ["started", "result"]
    assert lines[-1]["status"] == "completed"


def test_active_lists_the_recent_runs_hermes_still_runs(relay) -> None:
    run, tmp_path = relay
    assert run("active") == [{"type": "active", "run_ids": []}]
    for key in ("turn-1", "turn-2", "turn-3"):
        run("run", _request(tmp_path, key))
    FakeHermes.statuses = {"run_1": "running", "run_2": "missing", "run_3": "queued"}
    old = tmp_path / ".aurum" / "runs" / "stale"
    old.write_text("run_9")
    os.utime(old, (0, 0))
    FakeHermes.statuses["run_9"] = "running"

    [line] = run("active")

    assert line["type"] == "active" and sorted(line["run_ids"]) == ["run_1", "run_3"]
