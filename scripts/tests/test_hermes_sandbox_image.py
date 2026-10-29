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
    clarified: list[tuple[str, dict]] = []

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
        if self.path.endswith("/clarify"):
            FakeHermes.clarified.append((self.path, body))
            if body["request_id"] == "settled":
                return self._send(409, b'{"error": {"code": "stale_generation"}}')
            return self._send(200, b'{"status": "resolved"}')
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
    FakeHermes.clarified = []
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
        "HERMES_RELAY_BUNDLED_SKILLS": str(tmp_path / "bundled"),
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


def test_clarify_answers_a_pending_question_from_a_file_it_removes(relay, tmp_path: Path) -> None:
    run, _ = relay
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"request_id": "p1", "execution_generation": 1, "answer": "Rome"}))
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps({"request_id": "settled", "execution_generation": 1, "answer": "x"}))

    assert run("clarify", "run_1", str(answer)) == [{"type": "clarify", "status": 200}]
    assert run("clarify", "run_1", str(stale)) == [{"type": "clarify", "status": 409}]
    assert FakeHermes.clarified[0] == (
        "/v1/runs/run_1/clarify",
        {"request_id": "p1", "execution_generation": 1, "answer": "Rome"},
    )
    assert not answer.exists() and not stale.exists()


def test_memory_is_written_and_read_back_only_from_regular_files(relay, tmp_path: Path) -> None:
    run, _ = relay
    memories = tmp_path / ".hermes" / "memories"
    request = tmp_path / "memory.json"
    request.write_text(json.dumps({"memory": "repo uses uv", "user": "likes tea"}))

    assert run("memory-write", str(request)) == [{"type": "memory-written"}]
    assert not request.exists()
    assert (memories / "MEMORY.md").read_text() == "repo uses uv"
    assert oct((memories / "USER.md").stat().st_mode & 0o777) == "0o600"
    assert run("memory-read") == [{"type": "memory", "memory": "repo uses uv", "user": "likes tea"}]

    # The terminal can replace the files: a link, a FIFO or invalid text is not memory.
    (memories / "USER.md").unlink()
    (memories / "USER.md").symlink_to(tmp_path / ".hermes" / "config.yaml")
    (memories / "MEMORY.md").write_bytes(b"\xff\xfe not text")
    assert run("memory-read") == [{"type": "memory", "memory": None, "user": None}]
    (memories / "USER.md").unlink()
    os.mkfifo(memories / "USER.md")
    (memories / "MEMORY.md").write_text("x" * 100_000)
    [read] = run("memory-read")
    assert read["user"] is None and read["memory"] is None


def _skill(directory: Path, body: str = "Steps.", **extra: str) -> None:
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(f"---\nname: {directory.name}\ndescription: d\n---\n{body}\n")
    for name, text in extra.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(text)


def test_skills_read_returns_only_what_differs_from_the_bundled_skills(relay, tmp_path: Path) -> None:
    run, _ = relay
    skills, bundled = tmp_path / ".hermes" / "skills", tmp_path / "bundled"
    assert run("skills-read") == [{"type": "skills", "count": 0, "skipped": 0}]

    for root in (skills, bundled):
        _skill(root / "research" / "arxiv")
        _skill(root / "notes" / "standup", "Bundled steps.")
    (skills / ".bundled_manifest").write_text("arxiv:abc")
    (skills / ".hub").mkdir()
    (skills / "notes" / "standup" / "SKILL.md").write_text("---\nname: standup\ndescription: d\n---\nChanged.\n")
    _skill(skills / "mine", "Mine.", **{"scripts/run.py": "print(1)\n"})
    (skills / "mine" / "__pycache__").mkdir()
    (skills / "mine" / "__pycache__" / "run.pyc").write_bytes(b"\x00\x01")

    *found, summary = run("skills-read")

    assert summary == {"type": "skills", "count": 2, "skipped": 0}
    assert {item["path"]: {f["path"]: f["content"] for f in item["files"]} for item in found} == {
        "mine": {"SKILL.md": "---\nname: mine\ndescription: d\n---\nMine.\n", "scripts/run.py": "print(1)\n"},
        "notes/standup": {"SKILL.md": "---\nname: standup\ndescription: d\n---\nChanged.\n"},
    }


def test_skills_read_skips_links_binaries_and_oversized_skills(relay, tmp_path: Path) -> None:
    run, _ = relay
    skills = tmp_path / ".hermes" / "skills"
    _skill(skills / "linked", **{"config.yaml": "x"})
    (skills / "linked" / "config.yaml").unlink()
    (skills / "linked" / "config.yaml").symlink_to(tmp_path / ".hermes" / "config.yaml")
    _skill(skills / "binary")
    (skills / "binary" / "blob.bin").write_bytes(b"\x7fELF\x00")
    _skill(skills / "big", **{"a.md": "x" * 60_000, "b.md": "y" * 60_000})
    _skill(skills / "elsewhere")
    (skills / "outside").symlink_to(skills / "elsewhere", target_is_directory=True)

    *found, summary = run("skills-read")

    assert [item["path"] for item in found] == ["elsewhere"]
    assert summary == {"type": "skills", "count": 1, "skipped": 3}


def test_skills_read_reads_the_most_recently_changed_skills_first(relay, tmp_path: Path) -> None:
    run, _ = relay
    skills = tmp_path / ".hermes" / "skills"
    for index in range(17):
        _skill(skills / f"s{index:02d}")
        os.utime(skills / f"s{index:02d}" / "SKILL.md", (1_000 + index, 1_000 + index))
        os.utime(skills / f"s{index:02d}", (1_000 + index, 1_000 + index))
    # The alphabetically first skill changed last.
    (skills / "s00" / "notes.md").write_text("new")

    *found, summary = run("skills-read")

    assert found[0]["path"] == "s00" and "s01" not in [item["path"] for item in found]
    assert summary == {"type": "skills", "count": 16, "skipped": 1}


def test_skills_read_takes_no_linked_skills_directory(relay, tmp_path: Path) -> None:
    run, _ = relay
    _skill(tmp_path / "real" / "mine")
    (tmp_path / ".hermes" / "skills").symlink_to(tmp_path / "real", target_is_directory=True)

    assert run("skills-read") == [{"type": "skills", "count": 0, "skipped": 0}]


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
